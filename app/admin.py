"""Owner dashboard: first-run setup, plans, settings, guests. Served on its
own port so it can be kept off the guest network."""
import re
import secrets

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from datetime import datetime

from . import access, billing, db, freetier, promo, tls
from .gateway import get_driver, speed_tiers, valid_mac
from .templating import STATIC_DIR, TIMEZONES, render
from .util import check_pw, fmt_ts, hash_pw, ip_in, tz

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

SETTING_FIELDS = [
    # (key, label, kind, help)
    ("site_name", "Network name shown to guests", "text", ""),
    ("welcome_text", "Welcome message", "textarea", ""),
    ("support_text", "Help / contact line", "text", "e.g. Questions? Ask at the front office."),
    ("currency", "Currency", "text", "Three-letter code: cad, usd, eur…"),
    ("timezone", "Time zone", "tz", "Used for pass end times and the season end date."),
    ("season_end", "Season ends", "date", "Subscriptions stop billing after this day. Blank = no end."),
    ("portal_url", "Portal address", "text", "What guests get sent to, e.g. http://wifi.lake.lan. "
                                              "Use a hostname, not a bare IP — Stripe prefers it."),
    ("guest_subnet", "Guest network", "text", "e.g. 10.50.0.0/22. The admin page refuses these addresses."),
    ("admin_allowed_cidrs", "Admin allowed from", "text", "Optional, comma-separated, e.g. 192.168.1.0/24"),
    ("stripe_secret_key", "Stripe secret key", "secret", "sk_live_… or sk_test_… (a restricted key works too)"),
    ("stripe_webhook_secret", "Stripe webhook secret", "secret", "Optional. Only if Stripe can reach this portal."),
    ("gateway_driver", "Router", "select:dryrun=Dry run (control nothing)|opnsense=OPNsense|openwrt=OpenWrt",
     "Dry run lets you try PassGate without touching any router."),
    ("opnsense_alias", "Allow-list name", "text",
     "The list PassGate fills with paid guests: the OPNsense alias or OpenWrt set, e.g. paid_clients"),
    ("opnsense_url", "OPNsense address", "text", "e.g. https://10.51.0.1"),
    ("opnsense_key", "OPNsense API key", "secret", ""),
    ("opnsense_secret", "OPNsense API secret", "secret", ""),
    ("opnsense_verify_tls", "Verify OPNsense certificate", "select:0=No (self-signed)|1=Yes", ""),
    ("openwrt_url", "OpenWrt address", "text", "The router as PassGate reaches it, e.g. http://192.168.1.1"),
    ("openwrt_user", "OpenWrt user", "text", "Created by setup.sh (default passgate). Not root."),
    ("openwrt_password", "OpenWrt password", "secret", "The PASSWORD you gave setup.sh"),
    ("openwrt_verify_tls", "Verify OpenWrt certificate", "select:0=No (self-signed or http)|1=Yes", ""),
    ("swap_cooldown_minutes", "Device swap cooldown (minutes)", "number",
     "After a guest removes or swaps a device, how long before they can swap again. 0 = off. "
     "Stops people sharing one login by swapping back and forth."),
    ("guest_manual_mac", "Guests can add devices by MAC", "select:1=Yes (for TVs and consoles)|0=No (owner only)",
     "A typed-in MAC can't be moved off another account, but MACs can be copied — see the README."),
    ("speed_tiers", "Speed tiers (optional)", "textarea",
     "One per line, Name = alias (OPNsense) or set (OpenWrt), slowest first. Example:\nBasic = paid_basic\nPremium = paid_premium\n"
     "Then pick a tier on each plan and add the speed rules on your router. Leave blank if you don't use tiers."),
    ("checkout_grace_minutes", "Minutes of internet while paying", "number",
     "Guests need to reach Stripe's page before they've paid."),
    ("checkout_grants_per_day", "Checkout attempts per device per day", "number", ""),
    ("subscription_grace_hours", "Subscription grace period (hours)", "number",
     "Keeps people online while Stripe retries a failed renewal."),
]


def admin_ok(request: Request) -> bool:
    return request.session.get("admin") is True


def csrf_ok(request: Request, token: str) -> bool:
    return bool(token) and secrets.compare_digest(token, request.session.get("csrf", ""))


@app.middleware("http")
async def network_guard(request: Request, call_next):
    ip = request.client.host if request.client else ""
    allowed = db.get("admin_allowed_cidrs")
    if ip_in(ip, db.get("guest_subnet")) and not ip_in(ip, allowed):
        return PlainTextResponse("Not available on the guest network.", status_code=403)
    if allowed and not ip_in(ip, allowed) and ip not in ("127.0.0.1", "::1"):
        return PlainTextResponse("Not allowed from this address.", status_code=403)
    return await call_next(request)


def ensure_csrf(request):
    if not request.session.get("csrf"):
        request.session["csrf"] = secrets.token_urlsafe(24)


def guard(request):
    """Returns a redirect if the caller isn't logged in / setup isn't done."""
    ensure_csrf(request)
    if db.get("setup_complete") != "1":
        return RedirectResponse("/setup", status_code=303)
    if not admin_ok(request):
        return RedirectResponse("/login", status_code=303)
    return None


# ---- first run ---------------------------------------------------------------

@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    ensure_csrf(request)
    if db.get("setup_complete") == "1":
        return RedirectResponse("/", status_code=303)
    return render("admin/setup.html", request=request, s=db.all_settings())


@app.post("/setup")
async def setup_save(request: Request):
    if db.get("setup_complete") == "1":
        return RedirectResponse("/", status_code=303)
    f = await request.form()
    if not csrf_ok(request, f.get("csrf", "")):
        return PlainTextResponse("Session expired, go back and try again.", status_code=400)
    pw = f.get("admin_password", "")
    if len(pw) < 8 or pw != f.get("admin_password2"):
        return render("admin/setup.html", request=request, s={**db.all_settings(), **f},
                      err="Admin password must be at least 8 characters and match.")
    if f.get("timezone") not in TIMEZONES:
        f = {**f, "timezone": "UTC"}
    for key in ("site_name", "currency", "timezone", "season_end", "portal_url", "guest_subnet",
                "stripe_secret_key"):
        if key in f:
            db.set(key, f.get(key, "").strip())
    db.set("admin_pw_hash", hash_pw(pw))
    if not db.q("SELECT id FROM plans"):
        # Starter plans the owner can edit or delete.
        starters = [
            ("Day Pass", "24 hours from purchase.", 1000, "one_time", "hours", 24, "month", 1, 2, 1),
            ("Weekend Pass", "72 hours from purchase.", 2500, "one_time", "hours", 72, "month", 1, 2, 2),
            ("Monthly", "Renews every month until the end of the season. Cancel anytime.",
             6000, "subscription", "hours", 720, "month", 1, 3, 3),
        ]
        for p in starters:
            db.run("INSERT INTO plans (name, description, price_cents, kind, duration_mode, duration_hours, "
                   "interval, interval_count, max_devices, sort) VALUES (?,?,?,?,?,?,?,?,?,?)", p)
    db.set("setup_complete", "1")
    request.session["admin"] = True
    db.log("setup", "first-run setup completed")
    return RedirectResponse("/plans?msg=welcome", status_code=303)


# ---- login -----------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    ensure_csrf(request)
    if db.get("setup_complete") != "1":
        return RedirectResponse("/setup", status_code=303)
    return render("admin/login.html", request=request)


@app.post("/login")
def login(request: Request, password: str = Form(...), csrf: str = Form("")):
    if csrf_ok(request, csrf) and check_pw(password, db.get("admin_pw_hash")):
        request.session["admin"] = True
        return RedirectResponse("/", status_code=303)
    db.log("admin_login_failed", request.client.host if request.client else "")
    return render("admin/login.html", request=request, err="Wrong password.")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---- dashboard ---------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if (r := guard(request)):
        return r
    t = db.now()
    stats = {
        "online_users": db.one("SELECT COUNT(DISTINCT user_id) n FROM passes WHERE status='active' "
                               "AND starts_at<=? AND ends_at>?", (t, t))["n"],
        "devices": db.one("SELECT COUNT(*) n FROM devices d WHERE EXISTS (SELECT 1 FROM passes p "
                          "WHERE p.user_id=d.user_id AND p.status='active' AND p.starts_at<=? AND p.ends_at>?)",
                          (t, t))["n"],
        "subs": db.one("SELECT COUNT(*) n FROM passes WHERE kind='subscription' AND status='active'")["n"],
        "rev_30": db.one("SELECT COALESCE(SUM(amount_cents),0) n FROM passes WHERE source='stripe' "
                         "AND status!='pending' AND created_at>?", (t - 30 * 86400,))["n"],
        "rev_all": db.one("SELECT COALESCE(SUM(amount_cents),0) n FROM passes WHERE source='stripe' "
                          "AND status!='pending'")["n"],
    }
    events = db.q("SELECT * FROM events ORDER BY id DESC LIMIT 40")
    warnings = []
    if not billing.configured():
        warnings.append("Stripe isn't connected yet — guests can't pay. Add your key in Settings.")
    if db.get("gateway_driver") == "dryrun":
        warnings.append("Router is in dry-run mode — payments work but nobody is actually let online. "
                        "Choose your router in Settings when your firewall is ready.")
    if freetier.enabled():
        warnings.append(f"The free basic tier is on ({len(freetier.active_devices())} devices using it now). "
                        f"Make sure your router slows the '{freetier.alias()}' list, or free guests get full speed.")
    if promo.active():
        warnings.append(f"A free-access promotion is running until {fmt_ts(promo.active()[1])} "
                        f"({len(promo.devices())} devices connected free). Manage it under Promotions.")
    if tls.mode() != "off" and not tls.active():
        warnings.append("HTTPS is set up but there's no certificate yet. See the HTTPS page.")
    elif tls.active() and (tls.cert_info() or {}).get("days_left", 99) < 14:
        warnings.append("The HTTPS certificate expires in under 2 weeks. Check the HTTPS page.")
    if not db.get("portal_url"):
        warnings.append("Set the portal address in Settings so phones get redirected correctly.")
    return render("admin/dashboard.html", request=request, stats=stats, events=events, warnings=warnings,
                  msg=request.query_params.get("msg", ""))


@app.post("/sync")
def force_sync(request: Request, csrf: str = Form("")):
    if (r := guard(request)) or not csrf_ok(request, csrf):
        return r or RedirectResponse("/", status_code=303)
    try:
        billing.reconcile()
    except Exception:
        pass
    res = access.sync()
    msg = res.get("error") or f"Synced: {res.get('online', 0)} online, +{len(res.get('added', []))} " \
                              f"-{len(res.get('removed', []))}"
    return RedirectResponse("/?msg=" + msg.replace(" ", "+"), status_code=303)


# ---- plans -------------------------------------------------------------------

@app.get("/plans", response_class=HTMLResponse)
def plans_page(request: Request, msg: str = ""):
    if (r := guard(request)):
        return r
    return render("admin/plans.html", request=request, plans=db.q("SELECT * FROM plans ORDER BY sort, id"),
                  welcome=(msg == "welcome"), msg="" if msg == "welcome" else msg)


@app.get("/plans/{pid}", response_class=HTMLResponse)
def plan_edit(request: Request, pid: int):
    if (r := guard(request)):
        return r
    plan = db.one("SELECT * FROM plans WHERE id=?", (pid,)) if pid else None
    return render("admin/plan_edit.html", request=request, p=plan, tiers=speed_tiers())


@app.post("/plans/{pid}")
async def plan_save(request: Request, pid: int):
    if (r := guard(request)):
        return r
    f = await request.form()
    if not csrf_ok(request, f.get("csrf", "")):
        return RedirectResponse("/plans", status_code=303)
    if f.get("delete"):
        db.run("UPDATE plans SET active=0 WHERE id=?", (pid,))  # keep history, just hide
        db.run("DELETE FROM plans WHERE id=? AND NOT EXISTS (SELECT 1 FROM passes WHERE plan_id=?)", (pid, pid))
        return RedirectResponse("/plans?msg=Plan+removed", status_code=303)

    def num(k, d=0):
        try:
            return int(float(f.get(k, d)))
        except (TypeError, ValueError):
            return d
    try:
        price = round(float(f.get("price", "0")) * 100)
    except ValueError:
        price = 0
    kind = "subscription" if f.get("kind") == "subscription" else "one_time"
    duration_hours = num("duration_value", 1) * (24 if f.get("duration_unit") == "days" else 1)
    vals = (f.get("name", "Pass").strip()[:60] or "Pass", f.get("description", "").strip()[:300], max(price, 0),
            kind, "season" if f.get("duration_mode") == "season" else "hours", max(duration_hours, 1),
            f.get("interval") if f.get("interval") in ("day", "week", "month", "year") else "month",
            max(num("interval_count", 1), 1), 1 if f.get("stop_at_season_end") else 0,
            min(max(num("max_devices", 2), 1), 50), 1 if f.get("active") else 0, num("sort", 0),
            f.get("speed_tier", "").strip()[:31])

    def cents(k):
        try:
            return max(round(float(f.get(k) or 0) * 100), 0)
        except ValueError:
            return 0
    # extra device slots (all optional; price 0 = not sold)
    mode = f.get("extra_discount_mode") if f.get("extra_discount_mode") in ("percent", "amount") else "none"
    disc = min(max(num("extra_discount_pct", 0), 0), 100) if mode == "percent" else cents("extra_discount_amt")
    vals += (cents("extra_price") if f.get("extras_on") else 0, min(max(num("max_extra_devices", 3), 1), 50),
             mode, disc, cents("extra_min"))
    if price < 50:
        p = db.one("SELECT * FROM plans WHERE id=?", (pid,)) if pid else None
        return render("admin/plan_edit.html", request=request, p=p, tiers=speed_tiers(),
                      err="Stripe needs a price of at least $0.50. To give someone free access, "
                          "use 'Give a pass' on the Guests page.")
    cols = ("name, description, price_cents, kind, duration_mode, duration_hours, interval, "
            "interval_count, stop_at_season_end, max_devices, active, sort, speed_tier, "
            "extra_device_cents, max_extra_devices, extra_discount_mode, extra_discount_value, extra_min_cents")
    if pid:
        sets = ", ".join(f"{c.strip()}=?" for c in cols.split(","))
        db.run(f"UPDATE plans SET {sets} WHERE id=?", vals + (pid,))
    else:
        db.run(f"INSERT INTO plans ({cols}) VALUES ({','.join('?' * len(vals))})", vals)
    return RedirectResponse("/plans?msg=Saved", status_code=303)


# ---- settings ----------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = ""):
    if (r := guard(request)):
        return r
    return render("admin/settings.html", request=request, fields=SETTING_FIELDS, s=db.all_settings(), msg=msg)


@app.post("/settings")
async def settings_save(request: Request):
    if (r := guard(request)):
        return r
    f = await request.form()
    if not csrf_ok(request, f.get("csrf", "")):
        return RedirectResponse("/settings", status_code=303)
    for key, _, kind, _ in SETTING_FIELDS:
        if key not in f:
            continue
        val = f.get(key, "").strip()
        if kind == "secret" and val == "":
            continue  # blank = keep existing secret
        if kind == "secret" and val == "-":
            val = ""  # "-" clears it
        if kind == "tz" and val not in TIMEZONES:
            continue
        db.set(key, val)
    if f.get("new_admin_password"):
        if len(f["new_admin_password"]) >= 8:
            db.set("admin_pw_hash", hash_pw(f["new_admin_password"]))
        else:
            return RedirectResponse("/settings?msg=Admin+password+must+be+8%2B+characters", status_code=303)
    return RedirectResponse("/settings?msg=Saved", status_code=303)


@app.post("/settings/test")
def settings_test(request: Request, csrf: str = Form("")):
    if (r := guard(request)) or not csrf_ok(request, csrf):
        return r or RedirectResponse("/settings", status_code=303)
    results = []
    try:
        results.append("Router: " + get_driver().test())
    except Exception as e:
        results.append(f"Router: FAILED – {e}")
    if billing.configured():
        try:
            import stripe
            stripe.api_key = db.get("stripe_secret_key")
            acct = stripe.Balance.retrieve()
            mode = "live" if acct.get("livemode") else "test"
            results.append(f"Stripe: connected ({mode} mode)")
        except Exception as e:
            results.append(f"Stripe: FAILED – {e}")
    else:
        results.append("Stripe: no key entered")
    return render("admin/settings.html", request=request, fields=SETTING_FIELDS, s=db.all_settings(),
                  test_results=results)


# ---- guests ----------------------------------------------------------------

@app.get("/guests", response_class=HTMLResponse)
def guests(request: Request, q: str = ""):
    if (r := guard(request)):
        return r
    t = db.now()
    rows = db.q("""
        SELECT u.*, (SELECT MAX(ends_at) FROM passes p WHERE p.user_id=u.id AND p.status='active'
                     AND p.starts_at<=? AND p.ends_at>?) AS until,
               (SELECT COUNT(*) FROM devices d WHERE d.user_id=u.id) AS ndev
        FROM users u WHERE u.email LIKE ? ORDER BY until IS NULL, until DESC, u.created_at DESC LIMIT 300""",
                (t, t, f"%{q.strip().lower()}%"))
    return render("admin/guests.html", request=request, users=rows, q=q,
                  plans=db.q("SELECT * FROM plans ORDER BY sort, id"))


@app.get("/guests/{uid}", response_class=HTMLResponse)
def guest(request: Request, uid: int, msg: str = ""):
    if (r := guard(request)):
        return r
    u = db.one("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        return RedirectResponse("/guests", status_code=303)
    return render("admin/guest.html", request=request, u=u, msg=msg,
                  until=access.access_until(uid), limit=access.device_limit(uid),
                  cooldown_min=(access.cooldown_left(uid) + 59) // 60,
                  devices=access.devices(uid),
                  passes=db.q("SELECT * FROM passes WHERE user_id=? ORDER BY created_at DESC", (uid,)),
                  plans=db.q("SELECT * FROM plans ORDER BY sort, id"))


@app.post("/guests/new")
def guest_new(request: Request, email: str = Form(...), password: str = Form(...), csrf: str = Form("")):
    if (r := guard(request)) or not csrf_ok(request, csrf):
        return r or RedirectResponse("/guests", status_code=303)
    email = email.strip().lower()
    if db.one("SELECT id FROM users WHERE email=?", (email,)) or len(password) < 6:
        return RedirectResponse("/guests", status_code=303)
    uid = db.run("INSERT INTO users (email, pw_hash, created_at) VALUES (?,?,?)",
                 (email, hash_pw(password), db.now()))
    return RedirectResponse(f"/guests/{uid}?msg=Account+created", status_code=303)


@app.post("/guests/{uid}/action")
async def guest_action(request: Request, uid: int):
    if (r := guard(request)):
        return r
    f = await request.form()
    if not csrf_ok(request, f.get("csrf", "")):
        return RedirectResponse(f"/guests/{uid}", status_code=303)
    act, msg = f.get("action"), "Done"
    t = db.now()
    if act == "grant":
        plan = db.one("SELECT * FROM plans WHERE id=?", (int(f.get("plan_id", 0)),))
        try:
            hours = int(f.get("hours") or 0)
        except ValueError:
            hours = 0
        if plan:
            hours = hours or int(plan["duration_hours"])
            start = access.stacking_start(uid)
            db.run("INSERT INTO passes (user_id, plan_id, plan_name, kind, max_devices, status, source, "
                   "starts_at, ends_at, amount_cents, speed_tier, created_at) VALUES (?,?,?,?,?,?,?,?,?,0,?,?)",
                   (uid, plan["id"], plan["name"] + " (comp)", "one_time", plan["max_devices"], "active",
                    "admin", t, start + hours * 3600, plan["speed_tier"] or "", t))
            db.log("comp_pass", f"user={uid} hours={hours}")
            msg = f"Gave {hours} hours"
    elif act == "end_pass":
        db.run("UPDATE passes SET status='ended', ends_at=? WHERE id=? AND user_id=?",
               (t, int(f.get("pass_id", 0)), uid))
        msg = "Pass ended (a Stripe subscription keeps billing until you cancel it in Stripe)"
    elif act == "remove_device":
        access.remove_device(uid, int(f.get("device_id", 0)), by_admin=True)
        msg = "Device removed"
    elif act == "add_mac":
        mac = valid_mac(f.get("mac", ""))
        if not mac:
            msg = "That isn't a valid MAC address"
        else:
            ok, code = access.add_device(uid, mac, label=f.get("label", "").strip() or "Added by owner",
                                         source="manual", by_admin=True)
            msg = {"added": "Device added", "already": "Already on this account",
                   "moved": "Device added (it was moved off another account)",
                   "limit": "Account is at its device limit: remove one or raise the limit",
                   "noaccess": "Give them a pass first; devices only count while a pass is active"}.get(code, code)
    elif act == "set_limit":
        raw = (f.get("limit") or "").strip()
        if raw == "":
            db.run("UPDATE users SET device_limit_override=NULL WHERE id=?", (uid,))
            msg = "Device limit now follows their plan"
        else:
            try:
                n = min(max(int(raw), 1), 50)
                db.run("UPDATE users SET device_limit_override=? WHERE id=?", (n, uid))
                msg = f"Device limit set to {n}"
            except ValueError:
                msg = "Enter a number"
    elif act == "clear_cooldown":
        db.run("UPDATE users SET last_swap_at=NULL WHERE id=?", (uid,))
        msg = "They can swap devices now"
    elif act == "reset_pw":
        pw = f.get("password", "")
        if len(pw) >= 6:
            db.run("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(pw), uid))
            msg = "Password changed"
        else:
            msg = "Password must be 6+ characters"
    elif act == "toggle_disable":
        db.run("UPDATE users SET disabled=1-disabled WHERE id=?", (uid,))
        msg = "Updated"
    elif act == "notes":
        db.run("UPDATE users SET notes=? WHERE id=?", (f.get("notes", "")[:1000], uid))
        msg = "Notes saved"
    elif act == "refresh_stripe":
        for p in db.q("SELECT DISTINCT stripe_subscription_id s FROM passes WHERE user_id=? "
                      "AND stripe_subscription_id IS NOT NULL", (uid,)):
            try:
                billing.refresh_subscription(p["s"])
            except Exception as e:
                msg = f"Stripe error: {e}"
    access.sync()
    return RedirectResponse(f"/guests/{uid}?msg=" + msg.replace(" ", "+"), status_code=303)


# ---- HTTPS -------------------------------------------------------------------------

@app.get("/https", response_class=HTMLResponse)
def https_page(request: Request, msg: str = ""):
    if (r := guard(request)):
        return r
    keys = ("tls_domain", "tls_email", "tls_dns_provider", "tls_cloudflare_token", "tls_dns_env", "tls_staging")
    return render("admin/https.html", request=request, msg=msg, mode=tls.mode(), s={k: db.get(k) for k in keys},
                  info=tls.cert_info(), problems=tls.problems() if tls.mode() == "letsencrypt" else [],
                  last_result=db.get("tls_last_result") if tls.mode() == "letsencrypt" else "",
                  portal_url=tls.portal_url())


@app.post("/https")
async def https_save(request: Request):
    if (r := guard(request)):
        return r
    f = await request.form()
    if not csrf_ok(request, f.get("csrf", "")):
        return RedirectResponse("/https", status_code=303)
    mode = f.get("tls_mode") if f.get("tls_mode") in ("off", "letsencrypt", "files") else "off"
    changed = (mode, (f.get("tls_domain") or "").strip().lower()) != (tls.mode(), tls.domain())
    db.set("tls_mode", mode)
    db.set("tls_domain", (f.get("tls_domain") or "").strip().lower().rstrip(".")[:253])
    db.set("tls_email", (f.get("tls_email") or "").strip()[:254])
    provider = "cloudflare" if f.get("tls_dns_provider") != "other" else \
        (f.get("tls_dns_provider_other") or "").strip().lower()[:40]
    db.set("tls_dns_provider", provider)
    if f.get("tls_cloudflare_token"):
        db.set("tls_cloudflare_token", f["tls_cloudflare_token"].strip())
    if f.get("tls_dns_env"):
        db.set("tls_dns_env", f["tls_dns_env"].strip()[:4000])
    db.set("tls_staging", "1" if f.get("tls_staging") else "0")
    if changed:
        db.set("tls_last_check", "0")  # check straight away
        db.set("tls_last_result", "")
    msg = "Saved." + (" PassGate will request the certificate within a minute."
                      if mode == "letsencrypt" and not tls.active() and not tls.problems() else "")
    return RedirectResponse("/https?msg=" + msg.replace(" ", "+"), status_code=303)


@app.post("/https/issue")
def https_issue(request: Request, csrf: str = Form("")):
    if (r := guard(request)) or not csrf_ok(request, csrf):
        return r or RedirectResponse("/https", status_code=303)
    import threading
    db.set("tls_last_result", "Working on it… refresh this page in a minute.")
    threading.Thread(target=tls.obtain_or_renew, kwargs={"force": not tls.active()}, daemon=True).start()
    return RedirectResponse("/https", status_code=303)


@app.post("/https/upload")
async def https_upload(request: Request):
    if (r := guard(request)):
        return r
    f = await request.form()
    if not csrf_ok(request, f.get("csrf", "")):
        return RedirectResponse("/https", status_code=303)
    import os
    import ssl
    os.makedirs(tls.TLS_DIR, exist_ok=True)
    data = {}
    for field, path in (("cert", tls.OWN_CERT), ("key", tls.OWN_KEY)):
        up = f.get(field)
        if up is not None and getattr(up, "filename", ""):
            data[path] = await up.read()
    if not data:
        return RedirectResponse("/https?msg=Choose+a+file+to+upload", status_code=303)
    # Check the pair works before replacing what's there.
    cert = data.get(tls.OWN_CERT) or (open(tls.OWN_CERT, "rb").read() if os.path.exists(tls.OWN_CERT) else b"")
    key = data.get(tls.OWN_KEY) or (open(tls.OWN_KEY, "rb").read() if os.path.exists(tls.OWN_KEY) else b"")
    if cert and key:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            c, k = os.path.join(tmp, "c.pem"), os.path.join(tmp, "k.pem")
            open(c, "wb").write(cert)
            open(k, "wb").write(key)
            try:
                ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(c, k)
            except Exception as e:
                return RedirectResponse("/https?msg=" + f"Those files don't work together: {e}".replace(" ", "+")[:300],
                                        status_code=303)
    for path, content in data.items():
        with open(path, "wb") as fh:
            fh.write(content)
        os.chmod(path, 0o600)
    return RedirectResponse("/https?msg=Uploaded.+HTTPS+starts+within+30+seconds.", status_code=303)


# ---- free-access promotion ------------------------------------------------------

@app.get("/promo", response_class=HTMLResponse)
def promo_page(request: Request, msg: str = ""):
    if (r := guard(request)):
        return r
    w = promo.window()
    t = db.now()
    status = ("off" if db.get("promo_enabled") != "1" else "invalid" if not w
              else "scheduled" if t < w[0] else "active" if t < w[1] else "ended")
    s = {k: promo.get(k) for k in promo.SETTINGS}
    fs = {k: freetier.get(k) for k in freetier.SETTINGS}
    return render("admin/promo.html", request=request, s=s, status=status, window=w, msg=msg,
                  devices=promo.devices(), fs=fs, free_devices=freetier.active_devices())


@app.post("/freetier")
async def freetier_save(request: Request):
    if (r := guard(request)):
        return r
    f = await request.form()
    if not csrf_ok(request, f.get("csrf", "")):
        return RedirectResponse("/promo", status_code=303)
    if f.get("action") == "kick":
        freetier.stop(f.get("mac", ""))
        msg = "Device removed from the free tier for today."
    else:
        old_alias, was_on, old_minutes = freetier.alias(), freetier.enabled(), freetier.minutes()
        db.set("free_enabled", "1" if f.get("free_enabled") else "0")
        for k in ("free_title", "free_text", "free_speed_label"):
            db.set(k, f.get(k, "").strip()[:500])
        alias = f.get("free_alias", "").strip()
        if re.fullmatch(r"[A-Za-z0-9_]{1,31}", alias) and alias != access.base_alias():
            db.set("free_alias", alias)
        try:
            db.set("free_minutes", str(max(0, min(1440, int(f.get("free_minutes") or 0)))))
        except ValueError:
            pass
        if was_on and (not freetier.enabled() or freetier.alias() != old_alias):
            db.set("free_cleanup_alias", old_alias)   # empty the old slow list on the router
        if freetier.minutes() != old_minutes:
            freetier.reset()                          # new rules start everyone fresh
        msg = "Saved"
    access.sync()
    return RedirectResponse("/promo?msg=" + msg.replace(" ", "+") + "#free", status_code=303)


@app.post("/promo")
async def promo_save(request: Request):
    if (r := guard(request)):
        return r
    f = await request.form()
    if not csrf_ok(request, f.get("csrf", "")):
        return RedirectResponse("/promo", status_code=303)
    if f.get("action") == "end":
        db.set("promo_end", datetime.fromtimestamp(db.now() - 60, tz()).strftime("%Y-%m-%dT%H:%M"))
        msg = "Promotion ended. Free devices go back to the sign-in page within 30 seconds."
    else:
        if (f.get("promo_start"), f.get("promo_end")) != (db.get("promo_start"), db.get("promo_end")):
            promo.reset()  # a new promotion starts with a clean list
        db.set("promo_enabled", "1" if f.get("promo_enabled") else "0")
        for k in ("promo_start", "promo_end", "promo_title", "promo_text", "promo_after_text", "promo_after_days"):
            if k in f:
                db.set(k, f.get(k, "").strip()[:500])
        msg = "Saved"
        if db.get("promo_enabled") == "1" and not promo.window():
            msg = "Saved, but the end must be after the start, so the promotion won't run."
    access.sync()
    return RedirectResponse("/promo?msg=" + msg.replace(" ", "+"), status_code=303)


# ---- always-online (bypass) list ---------------------------------------------

@app.get("/bypass", response_class=HTMLResponse)
def bypass_page(request: Request, msg: str = ""):
    if (r := guard(request)):
        return r
    return render("admin/bypass.html", request=request, items=access.bypass_list(), msg=msg)


@app.post("/bypass")
def bypass_save(request: Request, csrf: str = Form(""), action: str = Form("add"), mac: str = Form(""),
                label: str = Form(""), bypass_id: int = Form(0)):
    if (r := guard(request)) or not csrf_ok(request, csrf):
        return r or RedirectResponse("/bypass", status_code=303)
    if action == "remove":
        access.remove_bypass(bypass_id)
        msg = "Removed"
    else:
        clean = valid_mac(mac)
        if not clean:
            return RedirectResponse("/bypass?msg=That+isn%27t+a+valid+MAC+address", status_code=303)
        access.add_bypass(clean, label.strip())
        msg = "Added"
    access.sync()
    return RedirectResponse("/bypass?msg=" + msg, status_code=303)


def build(secret: str):
    app.add_middleware(SessionMiddleware, secret_key=secret + "-admin", session_cookie="pg_admin",
                       max_age=60 * 60 * 12, same_site="strict", https_only=False)
    return app
