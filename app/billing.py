"""Stripe: checkout, fulfilment, and keeping subscriptions in sync.

Works without webhooks. Many ISPs put customers behind CGNAT, and many
owners would rather not open a port, so Stripe often can't reach in. PassGate
therefore checks with Stripe itself (outbound only):
  * right after the guest returns from checkout,
  * every few minutes for checkouts that never came back (closed browser),
  * every few minutes for subscriptions (renewed? cancelled? failed?).
If you do expose the portal publicly (e.g. Cloudflare Tunnel), webhooks
make all of this instant, but they're optional.
"""
import logging

import stripe

from . import db
from . import extras as xs
from .access import access_until, add_device, stacking_start, sync
from .util import season_end_ts

log = logging.getLogger("passgate.billing")


def configured() -> bool:
    return bool(db.get("stripe_secret_key"))


def _client():
    stripe.api_key = db.get("stripe_secret_key")
    return stripe


def _get(obj, key, default=None):
    try:
        v = obj[key]
        return default if v is None else v
    except (KeyError, TypeError, AttributeError):
        return getattr(obj, key, default)


def portal_base() -> str:
    return (db.get("portal_url") or "http://localhost").rstrip("/")


def _extras_product_data(n: int) -> dict:
    return {"name": f"{db.get('site_name')} – {n} extra device{'s' if n != 1 else ''}",
            "metadata": {"passgate": "extras", "count": str(n)}}


def create_checkout(user, plan, mac: str = None, ip: str = None, extra: int = 0) -> str:
    """Create a Stripe Checkout Session and return its URL. `extra` = paid
    device slots on top of what the plan includes."""
    s = _client()
    currency = db.get("currency").lower()
    product = {"name": f"{db.get('site_name')} – {plan['name']}"}
    if plan["description"]:
        product["description"] = plan["description"][:500]
    price_data = {"currency": currency, "unit_amount": int(plan["price_cents"]),
                  "product_data": product}
    params = {
        "line_items": [{"quantity": 1, "price_data": price_data}],
        "success_url": portal_base() + "/paid?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": portal_base() + "/",
        "client_reference_id": str(user["id"]),
        "metadata": {"user_id": str(user["id"]), "plan_id": str(plan["id"])},
    }
    if user["stripe_customer_id"]:
        params["customer"] = user["stripe_customer_id"]
    else:
        params["customer_email"] = user["email"]
    if plan["kind"] == "subscription":
        price_data["recurring"] = {"interval": plan["interval"],
                                   "interval_count": int(plan["interval_count"])}
        params["mode"] = "subscription"
        params["subscription_data"] = {"metadata": params["metadata"]}
    else:
        params["mode"] = "payment"
        if not user["stripe_customer_id"]:
            params["customer_creation"] = "always"
    extra = max(0, min(int(extra or 0), int(plan["max_extra_devices"] or 0))) if xs.enabled(plan) else 0
    extra_cents = xs.full_price(plan, 0, extra) if extra else 0
    if extra_cents:
        line = {"currency": currency, "unit_amount": extra_cents, "product_data": _extras_product_data(extra)}
        if plan["kind"] == "subscription":
            line["recurring"] = dict(price_data["recurring"])
        params["line_items"].append({"quantity": 1, "price_data": line})
        params["metadata"]["extra_devices"] = str(extra)
    sess = s.checkout.Session.create(**params)
    db.run("INSERT INTO passes (user_id, plan_id, plan_name, kind, max_devices, status, source, "
           "amount_cents, stripe_session_id, buy_mac, buy_ip, speed_tier, extra_devices, created_at) "
           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
           (user["id"], plan["id"], plan["name"], plan["kind"], plan["max_devices"], "pending",
            "stripe", plan["price_cents"] + extra_cents, sess["id"], mac, ip, plan["speed_tier"] or "",
            extra, db.now()))
    db.log("checkout_started", f"user={user['id']} plan={plan['name']} session={sess['id']}")
    return sess["url"]


def create_extras_checkout(user, n: int, mac: str = None, ip: str = None) -> str | None:
    """Buy n more device slots partway through a pass, prorated for the time
    left. For a subscription the slots are also added to the subscription so
    they renew with it."""
    quote = xs.quote_later(user["id"])
    if not quote or n < 1 or n > quote["max_more"]:
        return None
    cents = dict((o[0], o[1]) for o in quote["options"])[n]
    full = dict((o[0], o[2]) for o in quote["options"])[n]
    plan, parent = quote["plan"], quote["pass"]
    s = _client()
    desc = "Prorated for the time left on your pass."
    if quote["recurring"]:
        desc = f"Prorated until your next renewal, then {money_str(full)} per billing period with your plan."
    params = {
        "mode": "payment",
        "line_items": [{"quantity": 1, "price_data": {
            "currency": db.get("currency").lower(), "unit_amount": cents,
            "product_data": {**_extras_product_data(n), "description": desc}}}],
        "success_url": portal_base() + "/paid?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": portal_base() + "/account",
        "client_reference_id": str(user["id"]),
        "metadata": {"user_id": str(user["id"]), "kind": "extra_devices", "count": str(n),
                     "parent_pass_id": str(parent["id"])},
    }
    if user["stripe_customer_id"]:
        params["customer"] = user["stripe_customer_id"]
    else:
        params["customer_email"] = user["email"]
        params["customer_creation"] = "always"
    sess = s.checkout.Session.create(**params)
    db.run("INSERT INTO passes (user_id, plan_id, plan_name, kind, max_devices, status, source, amount_cents, "
           "stripe_session_id, buy_mac, buy_ip, extra_devices, parent_pass_id, created_at) "
           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
           (user["id"], plan["id"], f"{n} extra device{'s' if n != 1 else ''}", "addon", 0, "pending", "stripe",
            cents, sess["id"], mac, ip, n, parent["id"], db.now()))
    db.log("checkout_started", f"user={user['id']} extra_devices={n} session={sess['id']}")
    return sess["url"]


def money_str(cents):
    from .util import money
    return money(cents)


def _extras_product(s, n: int) -> str:
    """A Stripe Product for 'n extra devices', created once and reused
    (subscription items need a real product id)."""
    mode = "live" if db.get("stripe_secret_key").startswith(("sk_live", "rk_live")) else "test"
    key = f"stripe_extras_product_{mode}_{n}"
    pid = db.get(key)
    if not pid:
        prod = s.Product.create(**_extras_product_data(n))
        pid = prod["id"]
        db.set(key, pid)
    return pid


def _fulfil_addon(s, p, sess) -> None:
    parent = db.one("SELECT * FROM passes WHERE id=?", (p["parent_pass_id"],))
    t = db.now()
    n = int(p["extra_devices"])
    if parent and parent["kind"] == "subscription" and parent["stripe_subscription_id"]:
        # Renew the slots with the subscription from the next period on.
        # This period was already paid (prorated) through Checkout.
        plan = db.one("SELECT * FROM plans WHERE id=?", (parent["plan_id"],))
        held_before = int(parent["extra_devices"] or 0)
        recurring_cents = xs.full_price(plan, held_before, n) if plan else p["amount_cents"]
        added = True
        try:
            s.SubscriptionItem.create(
                subscription=parent["stripe_subscription_id"], quantity=1, proration_behavior="none",
                price_data={"currency": db.get("currency").lower(), "unit_amount": recurring_cents,
                            "product": _extras_product(s, n),
                            "recurring": {"interval": plan["interval"] if plan else "month",
                                          "interval_count": int(plan["interval_count"]) if plan else 1}})
        except Exception as e:
            log.error("couldn't add extra devices to subscription %s: %s", parent["stripe_subscription_id"], e)
            db.log("error", f"extra devices paid but not added to subscription {parent['stripe_subscription_id']}: {e}")
            added = False
        if added:
            # The subscription's own row now carries the slots (and keeps them on renewal).
            db.run("UPDATE passes SET extra_devices=COALESCE(extra_devices,0)+? WHERE id=?", (n, parent["id"]))
        # Otherwise the add-on row keeps them for the period they paid for.
        db.run("UPDATE passes SET status='active', starts_at=?, ends_at=?, extra_devices=? WHERE id=? "
               "AND status='pending'", (t, parent["ends_at"], 0 if added else n, p["id"]))
    else:
        end = access_until(p["user_id"]) or (parent["ends_at"] if parent else t)
        db.run("UPDATE passes SET status='active', starts_at=?, ends_at=? WHERE id=? AND status='pending'",
               (t, end, p["id"]))


def _sub_period_end(sub) -> int | None:
    # Newer Stripe API versions keep the period on the subscription item.
    end = _get(sub, "current_period_end")
    if not end:
        items = _get(_get(sub, "items", {}), "data", []) or []
        ends = [_get(i, "current_period_end") for i in items if _get(i, "current_period_end")]
        end = max(ends) if ends else None
    return int(end) if end else None


def fulfil_session(session_id: str) -> bool:
    """Turn a paid Checkout Session into access. Idempotent: safe to call
    from the success page, the poller and a webhook for the same session."""
    p = db.one("SELECT * FROM passes WHERE stripe_session_id=?", (session_id,))
    if not p:
        return False
    if p["status"] != "pending":
        return p["status"] == "active"
    s = _client()
    sess = s.checkout.Session.retrieve(session_id)
    paid = _get(sess, "payment_status") in ("paid", "no_payment_required") \
        or (_get(sess, "mode") == "subscription" and _get(sess, "status") == "complete")
    if not paid:
        return False

    customer = _get(sess, "customer")
    if customer:
        cid = customer if isinstance(customer, str) else _get(customer, "id")
        db.run("UPDATE users SET stripe_customer_id=? WHERE id=? AND stripe_customer_id IS NULL",
               (cid, p["user_id"]))

    plan = db.one("SELECT * FROM plans WHERE id=?", (p["plan_id"],))
    t = db.now()
    if p["kind"] == "addon":
        _fulfil_addon(s, p, sess)
    elif p["kind"] == "subscription":
        sub_id = _get(sess, "subscription")
        sub_id = sub_id if isinstance(sub_id, str) else _get(sub_id, "id")
        sub = s.Subscription.retrieve(sub_id)
        grace = db.get_int("subscription_grace_hours", 24) * 3600
        end = (_sub_period_end(sub) or t + 31 * 86400) + grace
        se = season_end_ts()
        if plan and plan["stop_at_season_end"] and se:
            try:
                s.Subscription.modify(sub_id, cancel_at=se)
            except Exception as e:
                log.warning("couldn't set season end on %s: %s", sub_id, e)
        db.run("UPDATE passes SET status='active', starts_at=?, ends_at=?, stripe_subscription_id=? "
               "WHERE id=? AND status='pending'", (t, end, sub_id, p["id"]))
    else:
        start = stacking_start(p["user_id"])
        if plan and plan["duration_mode"] == "season":
            end = season_end_ts() or start + int(plan["duration_hours"]) * 3600
        else:
            hours = int(plan["duration_hours"]) if plan else 24
            end = start + hours * 3600
        # Active from now; if stacked on an existing pass, its end is pushed out.
        db.run("UPDATE passes SET status='active', starts_at=?, ends_at=? WHERE id=? AND status='pending'",
               (t, end, p["id"]))
    db.log("payment", f"user={p['user_id']} plan={p['plan_name']} session={session_id}")
    if p["buy_mac"]:  # the device that paid gets online without logging in again
        # (if the account is already full, the guest is shown a swap choice
        # on the next page instead; see portal.paid)
        add_device(p["user_id"], p["buy_mac"], p["buy_ip"] or "", skip_cooldown=True)
        db.run("DELETE FROM temp_grants WHERE mac=?", (p["buy_mac"],))  # paid; the pass covers it now
    sync()
    return True


def _sub_extra_devices(sub) -> int | None:
    """Extra slots on a subscription, read from its items (products tagged
    passgate=extras). None if the products weren't expanded."""
    items = _get(_get(sub, "items", {}), "data", []) or []
    total, seen = 0, False
    for it in items:
        prod = _get(_get(it, "price", {}), "product")
        if isinstance(prod, str) or prod is None:
            continue
        seen = True
        meta = _get(prod, "metadata", {}) or {}
        if _get(meta, "passgate") == "extras":
            try:
                total += int(_get(meta, "count", 0)) * int(_get(it, "quantity", 1) or 1)
            except (TypeError, ValueError):
                pass
    return total if seen else None


def refresh_subscription(sub_id: str):
    s = _client()
    sub = s.Subscription.retrieve(sub_id, expand=["items.data.price.product"])
    status = _get(sub, "status")
    grace = db.get_int("subscription_grace_hours", 24) * 3600
    t = db.now()
    if status in ("active", "trialing", "past_due"):
        end = (_sub_period_end(sub) or t) + grace
        db.run("UPDATE passes SET ends_at=?, status='active' WHERE stripe_subscription_id=? "
               "AND status IN ('active','ended')", (end, sub_id))
        n = _sub_extra_devices(sub)
        if n is not None:  # follows Stripe, e.g. if the owner removed an item there
            db.run("UPDATE passes SET extra_devices=? WHERE stripe_subscription_id=? AND kind='subscription'",
                   (n, sub_id))
    else:  # canceled, unpaid, incomplete_expired, paused
        ended = _get(sub, "ended_at") or t
        db.run("UPDATE passes SET ends_at=MIN(ends_at, ?), status='ended' "
               "WHERE stripe_subscription_id=? AND status='active'", (int(ended), sub_id))
    return status


def reconcile():
    """Catch anything the success page or webhooks missed."""
    if not configured():
        return
    t = db.now()
    for p in db.q("SELECT stripe_session_id FROM passes WHERE status='pending' "
                  "AND stripe_session_id IS NOT NULL AND created_at>?", (t - 86400,)):
        try:
            fulfil_session(p["stripe_session_id"])
        except Exception as e:
            log.warning("session check failed: %s", e)
    subs = db.q("SELECT DISTINCT stripe_subscription_id AS s FROM passes WHERE stripe_subscription_id "
                "IS NOT NULL AND (status='active' OR ends_at>?)", (t - 7 * 86400,))
    for r in subs:
        try:
            refresh_subscription(r["s"])
        except Exception as e:
            log.warning("subscription check failed: %s", e)
    sync()


def handle_webhook(payload: bytes, sig: str):
    secret = db.get("stripe_webhook_secret")
    if not secret:
        raise ValueError("webhook secret not configured")
    _client()
    event = stripe.Webhook.construct_event(payload, sig, secret)
    typ, obj = event["type"], event["data"]["object"]
    if typ.startswith("checkout.session."):
        fulfil_session(obj["id"])
    elif typ.startswith("customer.subscription."):
        refresh_subscription(obj["id"])
    elif typ.startswith("invoice."):
        sub = _get(obj, "subscription") or _get(_get(_get(obj, "parent", {}), "subscription_details", {}), "subscription")
        if sub:
            refresh_subscription(sub if isinstance(sub, str) else sub["id"])
    sync()
    return typ


def billing_portal_url(user) -> str | None:
    if not (configured() and user["stripe_customer_id"]):
        return None
    try:
        s = _client()
        bp = s.billing_portal.Session.create(customer=user["stripe_customer_id"],
                                             return_url=portal_base() + "/account")
        return bp["url"]
    except Exception as e:
        log.warning("billing portal unavailable: %s", e)
        return None
