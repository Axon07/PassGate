"""The guest-facing captive portal (what pops up on someone's phone)."""
import logging
import re
from urllib.parse import urlparse

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import access, billing, db
from . import extras as xs
from . import promo, tls
from .templating import STATIC_DIR, render
from .gateway import valid_mac
from .util import check_pw, hash_pw

log = logging.getLogger("passgate.portal")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "0.0.0.0"


def current_user(request: Request):
    uid = request.session.get("uid")
    if not uid:
        return None
    return db.one("SELECT * FROM users WHERE id=? AND disabled=0", (uid,))


@app.middleware("http")
async def captive_redirect(request: Request, call_next):
    """Phones probe sites like captive.apple.com. The router forwards those
    requests here; anything not addressed to the portal gets bounced to it,
    which is what makes the 'Sign in to network' sheet appear."""
    path = request.url.path
    if path.startswith(("/static", "/stripe/webhook", "/api/captive", "/healthz")):
        return await call_next(request)
    portal = tls.portal_url()
    if portal:
        want = (urlparse(portal).hostname or "").lower()
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if want and host and host != want:
            return RedirectResponse(portal.rstrip("/") + "/", status_code=302)
        if portal.startswith("https://") and request.url.scheme == "http":
            # Phones find the portal over plain HTTP (that's how captive
            # detection works); everything after that happens over HTTPS.
            target = portal.rstrip("/") + request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(target, status_code=302 if request.method in ("GET", "HEAD") else 307)
    if db.get("setup_complete") != "1":
        return render("message.html", status_code=503, title="Almost ready",
                      message="This Wi-Fi portal hasn't been set up yet.")
    return await call_next(request)


# ---- pages ------------------------------------------------------------------

def plans():
    return db.q("SELECT * FROM plans WHERE active=1 ORDER BY sort, price_cents")


@app.get("/", response_class=HTMLResponse)
def home(request: Request, msg: str = "", err: str = ""):
    user = current_user(request)
    ip = client_ip(request)
    mac, device_owner = access.identify(ip)
    if user:
        return RedirectResponse("/account", status_code=303)
    if device_owner and access.has_access(device_owner):
        dev = db.one("SELECT * FROM devices WHERE mac=?", (mac,))
        if dev and dev["source"] == "invite":
            # Joined with a code (e.g. a kid's phone): show status only, never
            # the parent's account, billing or password.
            return _joined_page(device_owner, dev)
        # A device already registered to an account with time left is online:
        # just show the account (the session cookie may have been lost).
        request.session["uid"] = device_owner
        return RedirectResponse("/account", status_code=303)
    return render("home.html", request=request, plans=plans(), msg=msg, err=err,
                  device_known=bool(mac), xs=xs, **promo_ctx(mac))


def promo_ctx(mac):
    """What the sign-in page should say about a free-access promotion."""
    act = promo.active()
    return {"promo_on": bool(act), "promo_until": act[1] if act else None,
            "promo_mine": promo.claimed(mac) if act else False,
            "promo_thanks": promo.show_thanks(mac),
            "promo_title": promo.get("promo_title"), "promo_text": promo.get("promo_text"),
            "promo_after_text": promo.get("promo_after_text")}


@app.post("/free")
def free(request: Request):
    """One tap during a promotion: this device is online until it ends."""
    ip = client_ip(request)
    mac = access.identify(ip)[0]
    if not mac:
        return RedirectResponse("/?err=Couldn%27t+identify+this+device.+Reconnect+to+the+Wi-Fi+and+try+again.",
                                status_code=303)
    if not promo.claim(mac, ip):
        return RedirectResponse("/?err=The+free+access+period+has+ended.", status_code=303)
    access.sync()
    return RedirectResponse("/?msg=free", status_code=303)


def _joined_page(owner_id, dev, fresh=False):
    owner = db.one("SELECT email FROM users WHERE id=?", (owner_id,))
    name, _, domain = owner["email"].partition("@")
    masked = (name[:2] + "•••@" + domain) if owner else "another account"
    return render("joined.html", owner=masked, until=access.access_until(owner_id), dev=dev, fresh=fresh)


@app.post("/join")
def join(request: Request, code: str = Form(...)):
    """A device joins someone else's account with a code they made."""
    ip = client_ip(request)
    mac = access.identify(ip)[0]
    if not mac:
        return render("home.html", request=request, plans=plans(), xs=xs, show_join=True,
                      err="Couldn't identify this device. Reconnect to the Wi-Fi and try again.")
    ok, res = access.redeem_invite(code, mac, ip, client=mac)
    if ok:
        request.session.clear()  # this device isn't logged in to the owner's account
        access.sync()
        return _joined_page(res, db.one("SELECT * FROM devices WHERE mac=?", (mac,)), fresh=True)
    err = {"bad_code": "That code didn't work. Check it and try again. Codes work once and expire after a day.",
           "slow_down": "Too many wrong codes. Wait a while and try again.",
           "noaccess": "The account that made this code doesn't have an active pass.",
           "limit": "That account has no free device slots. Ask them to free one up or add a slot, "
                    "then make a new code."}.get(res, "Couldn't join with that code.")
    return render("home.html", request=request, plans=plans(), xs=xs, show_join=True, err=err)


@app.post("/start")
def start(request: Request, plan_id: int = Form(...), email: str = Form(""),
          password: str = Form(""), extra: int = Form(0)):
    """Pick a plan → create or log in to an account → Stripe Checkout."""
    plan = db.one("SELECT * FROM plans WHERE id=? AND active=1", (plan_id,))
    if not plan:
        return RedirectResponse("/?err=That+plan+isn%27t+available", status_code=303)
    user = current_user(request)
    if not user:
        email = email.strip().lower()
        if not EMAIL_RE.match(email) or len(password) < 6:
            return render("home.html", request=request, plans=plans(), chosen=plan_id, email=email, xs=xs,
                          err="Enter your email and a password of at least 6 characters.")
        user = db.one("SELECT * FROM users WHERE email=?", (email,))
        if user:
            if user["disabled"] or not check_pw(password, user["pw_hash"]):
                return render("home.html", request=request, plans=plans(), chosen=plan_id, email=email, xs=xs,
                              err="That email already has an account and the password didn't match.")
        else:
            uid = db.run("INSERT INTO users (email, pw_hash, created_at) VALUES (?,?,?)",
                         (email, hash_pw(password), db.now()))
            db.log("signup", email)
            user = db.one("SELECT * FROM users WHERE id=?", (uid,))
        request.session["uid"] = user["id"]
    return _checkout(request, user, plan, extra=extra)


def _checkout(request, user, plan=None, extra: int = 0, slots: int = 0):
    """Start Stripe Checkout for a plan (optionally with extra device slots)
    or, with slots=n, for n more slots on the current pass."""
    if not billing.configured():
        return render("message.html", title="Payments not set up",
                      message="The owner hasn't connected Stripe yet. Please check back soon.")
    ip = client_ip(request)
    mac = access.identify(ip)[0]
    if not mac:
        return render("message.html", title="Couldn't identify your device",
                      message="Turn Wi-Fi off and on, reconnect to this network, and try again.")
    # Let the device reach Stripe's payment page for a few minutes.
    if not access.grant_checkout_access(mac, ip):
        return render("message.html", title="Too many attempts",
                      message="This device has started checkout too many times today. Please try again tomorrow.")
    try:
        if slots:
            url = billing.create_extras_checkout(user, slots, mac=mac, ip=ip)
            if not url:
                return RedirectResponse("/account?msg=noslots", status_code=303)
        else:
            url = billing.create_checkout(user, plan, mac=mac, ip=ip, extra=extra)
    except Exception as e:
        log.exception("checkout failed")
        return render("message.html", title="Payment error", message=f"Stripe said: {e}")
    return RedirectResponse(url, status_code=303)


@app.post("/buy")
def buy(request: Request, plan_id: int = Form(...), extra: int = Form(0)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/", status_code=303)
    plan = db.one("SELECT * FROM plans WHERE id=? AND active=1", (plan_id,))
    if not plan:
        return RedirectResponse("/account", status_code=303)
    return _checkout(request, user, plan, extra=extra)


@app.post("/slots")
def buy_slots(request: Request, n: int = Form(...)):
    """Add device slots to the pass they already have (prorated)."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/", status_code=303)
    return _checkout(request, user, slots=n)


@app.post("/invite")
def invite(request: Request, label: str = Form("")):
    user = current_user(request)
    if not user:
        return RedirectResponse("/", status_code=303)
    if not access.has_access(user["id"]):
        return RedirectResponse("/account?msg=noaccess", status_code=303)
    request.session["new_code"] = access.create_invite(user["id"], label.strip())
    return RedirectResponse("/account?msg=invite", status_code=303)


@app.post("/invite/cancel")
def invite_cancel(request: Request, invite_id: int = Form(...)):
    user = current_user(request)
    if user:
        access.cancel_invite(user["id"], invite_id)
    return RedirectResponse("/account", status_code=303)


@app.get("/paid", response_class=HTMLResponse)
def paid(request: Request, session_id: str = ""):
    ok = False
    try:
        ok = billing.fulfil_session(session_id)
    except Exception:
        log.exception("fulfil failed")
    if not ok:
        return render("message.html", title="Waiting for payment",
                      message="We haven't received confirmation from Stripe yet. This page will refresh.",
                      refresh=5)
    p = db.one("SELECT user_id, kind FROM passes WHERE stripe_session_id=?", (session_id,))
    request.session["uid"] = p["user_id"]
    paid_msg = "slots" if p["kind"] == "addon" else "paid"
    ip = client_ip(request)
    mac = access.identify(ip)[0]
    if not mac:
        return RedirectResponse("/account?msg=nodevice", status_code=303)
    done, code = access.add_device(p["user_id"], mac, ip, label=device_label(request), skip_cooldown=True)
    if done:
        access.sync()
        return RedirectResponse("/account?msg=" + paid_msg, status_code=303)
    # Paid from a new device while the account is already full: let them
    # swap straight away, without waiting out the cooldown.
    request.session["free_swap_until"] = db.now() + 15 * 60
    return RedirectResponse("/account?msg=paidlimit", status_code=303)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    user = db.one("SELECT * FROM users WHERE email=?", (email.strip().lower(),))
    if not user or user["disabled"] or not check_pw(password, user["pw_hash"]):
        return render("home.html", request=request, plans=plans(), login_email=email,
                      err="Email or password is wrong.", show_login=True)
    request.session["uid"] = user["id"]
    if access.has_access(user["id"]):
        return _add_this_device(request, user)
    return RedirectResponse("/account", status_code=303)


RESULT_MSG = {"added": "online", "already": "online", "moved": "moved", "limit": "limit",
              "cooldown": "cooldown", "noaccess": "noaccess", "taken": "taken"}


def _free_swap(request) -> bool:
    return request.session.get("free_swap_until", 0) > db.now()


def _add_this_device(request, user, replace_id=None):
    ip = client_ip(request)
    mac = access.identify(ip)[0]
    if not mac:
        return RedirectResponse("/account?msg=nodevice", status_code=303)
    ok, code = access.add_device(user["id"], mac, ip, label=device_label(request), replace_id=replace_id,
                                 skip_cooldown=_free_swap(request))
    if ok:
        request.session.pop("free_swap_until", None)
        access.sync()
    return RedirectResponse("/account?msg=" + RESULT_MSG.get(code, ""), status_code=303)


def device_label(request) -> str:
    ua = request.headers.get("user-agent", "")
    for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                      ("Macintosh", "Mac"), ("Windows", "Windows PC"), ("CrOS", "Chromebook"),
                      ("Linux", "Linux")):
        if key in ua:
            return name
    return "Device"


@app.get("/account", response_class=HTMLResponse)
def account(request: Request, msg: str = ""):
    user = current_user(request)
    if not user:
        return RedirectResponse("/", status_code=303)
    ip = client_ip(request)
    mac = access.identify(ip)[0]
    devs = access.devices(user["id"])
    this_on = any(d["mac"] == mac for d in devs)
    subs = db.q("SELECT * FROM passes WHERE user_id=? AND kind='subscription' AND status='active'",
                (user["id"],))
    wait = 0 if _free_swap(request) else access.cooldown_left(user["id"])
    extras_quote = xs.quote_later(user["id"]) if access.has_access(user["id"]) else None
    return render("account.html", request=request, user=user, plans=plans(), msg=msg,
                  until=access.access_until(user["id"]), limit=access.device_limit(user["id"]),
                  devices=devs, this_mac=mac, this_on=this_on, subs=subs,
                  cooldown_min=(wait + 59) // 60, quote=extras_quote, xs=xs,
                  invites=access.open_invites(user["id"]), new_code=request.session.pop("new_code", None)
                  if msg == "invite" else None, cooldown_setting=db.get_int("swap_cooldown_minutes", 60),
                  manual_mac=db.get("guest_manual_mac") == "1",
                  passes=db.q("SELECT * FROM passes WHERE user_id=? AND status!='pending' "
                              "ORDER BY created_at DESC LIMIT 10", (user["id"],)))


@app.post("/device/add")
def device_add(request: Request, replace_id: int = Form(0)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/", status_code=303)
    return _add_this_device(request, user, replace_id=replace_id or None)


@app.post("/device/manual")
def device_manual(request: Request, mac: str = Form(...), label: str = Form("")):
    """Add a device that can't open the sign-in page (TV, console, Roku)."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/", status_code=303)
    if db.get("guest_manual_mac") != "1":
        return RedirectResponse("/account", status_code=303)
    clean = valid_mac(mac)
    if not clean:
        return RedirectResponse("/account?msg=badmac", status_code=303)
    ok, code = access.add_device(user["id"], clean, label=label.strip() or "TV / console",
                                 source="manual", skip_cooldown=_free_swap(request))
    if ok:
        access.sync()
        code = "manual_ok" if code != "already" else "already"
    return RedirectResponse("/account?msg=" + {"manual_ok": "manualok"}.get(code, RESULT_MSG.get(code, "")),
                            status_code=303)


@app.post("/device/remove")
def device_remove(request: Request, device_id: int = Form(...)):
    user = current_user(request)
    if user:
        access.remove_device(user["id"], device_id)
        access.sync()
    return RedirectResponse("/account", status_code=303)


@app.post("/password")
def change_password(request: Request, current: str = Form(...), new: str = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/", status_code=303)
    if not check_pw(current, user["pw_hash"]) or len(new) < 6:
        return RedirectResponse("/account?msg=pwfail", status_code=303)
    db.run("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(new), user["id"]))
    return RedirectResponse("/account?msg=pwok", status_code=303)


@app.get("/manage")
def manage(request: Request):
    user = current_user(request)
    url = billing.billing_portal_url(user) if user else None
    if url:
        return RedirectResponse(url, status_code=303)
    return render("message.html", title="Not available",
                  message="Subscription management isn't available right now. Contact the owner to cancel.")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


# ---- machine endpoints -------------------------------------------------------

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    try:
        typ = billing.handle_webhook(await request.body(), request.headers.get("stripe-signature", ""))
    except Exception as e:
        log.warning("webhook rejected: %s", e)
        return PlainTextResponse("bad request", status_code=400)
    return {"received": typ}


@app.get("/api/captive")
def captive_api(request: Request):
    """RFC 8908 Captive Portal API (advertise it with DHCP option 114)."""
    _, uid = access.identify(client_ip(request))
    online = bool(uid and access.has_access(uid))
    out = {"captive": not online, "user-portal-url": (tls.portal_url() or "").rstrip("/") + "/"}
    if online:
        out["seconds-remaining"] = max(0, (access.access_until(uid) or 0) - db.now())
    return JSONResponse(out, media_type="application/captive+json")


@app.get("/healthz")
def health():
    return {"ok": True}


class SchemeAwareSessions:
    """Session cookies get the Secure flag when the request came in over
    HTTPS, so once HTTPS is on, logins are never sent in the clear."""

    def __init__(self, app, secret):
        kw = dict(secret_key=secret, session_cookie="pg_session", max_age=60 * 60 * 24 * 120, same_site="lax")
        self.plain = SessionMiddleware(app, https_only=False, **kw)
        self.secure = SessionMiddleware(app, https_only=True, **kw)

    async def __call__(self, scope, receive, send):
        target = self.secure if scope.get("scheme") == "https" else self.plain
        await target(scope, receive, send)


def build(secret: str):
    app.add_middleware(SchemeAwareSessions, secret=secret)
    return app
