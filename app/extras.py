"""Paid extra device slots: pricing, discounts and proration.

Each plan can sell extra slots on top of the devices it includes. The k-th
extra slot can be cheaper than the one before (percent or fixed amount off
per slot, never below a floor). Slots bought partway through a pass are
charged only for the time left.
"""
from . import db
from .access import access_until

INTERVAL_SECONDS = {"day": 86400, "week": 7 * 86400, "month": int(30.44 * 86400), "year": 365 * 86400}
STRIPE_MIN_CENTS = 50


def enabled(plan) -> bool:
    return bool(plan) and int(plan["extra_device_cents"] or 0) > 0 and int(plan["max_extra_devices"] or 0) > 0


def slot_price(plan, k: int) -> int:
    """Full-term price in cents of the k-th extra slot (k starts at 1)."""
    base = int(plan["extra_device_cents"] or 0)
    mode, val = plan["extra_discount_mode"] or "none", int(plan["extra_discount_value"] or 0)
    steps = max(0, k - 1)
    if mode == "percent" and val > 0:
        price = round(base * max(0.0, 1 - steps * val / 100))
    elif mode == "amount" and val > 0:
        price = base - steps * val
    else:
        return base
    return max(price, int(plan["extra_min_cents"] or 0), 0)


def full_price(plan, held: int, n: int) -> int:
    """Price for n more slots when `held` are already owned."""
    return sum(slot_price(plan, k) for k in range(held + 1, held + n + 1))


def price_list(plan, held: int = 0, n: int | None = None) -> list[int]:
    n = int(plan["max_extra_devices"] or 0) - held if n is None else n
    return [slot_price(plan, k) for k in range(held + 1, held + 1 + max(n, 0))]


def describe(plan) -> str:
    """Short guest-facing wording, e.g. '+$3 per extra device' or '+$3, then $2.70, $2.40…'."""
    from .util import money
    if not enabled(plan):
        return ""
    prices = price_list(plan, 0)
    if len(set(prices)) == 1:
        return f"extra devices {money(prices[0])} each"
    shown = ", ".join(money(p) for p in prices[:3])
    return f"extra devices {shown}{'…' if len(prices) > 3 else ''}"


# ---- buying slots partway through a pass ---------------------------------------

def held(user_id: int) -> int:
    t = db.now()
    r = db.one("SELECT COALESCE(SUM(extra_devices),0) AS n FROM passes WHERE user_id=? AND status='active' "
               "AND starts_at<=? AND ends_at>?", (user_id, t, t))
    return int(r["n"])


def governing_pass(user_id: int):
    """The pass whose plan sets the extra-slot price: the active pass (not an
    add-on) that ends last."""
    t = db.now()
    return db.one("SELECT * FROM passes WHERE user_id=? AND status='active' AND kind IN ('one_time','subscription') "
                  "AND starts_at<=? AND ends_at>? ORDER BY ends_at DESC LIMIT 1", (user_id, t, t))


def fraction_left(user_id: int, p, plan) -> float:
    """Share of a full term that's left, for fair (prorated) pricing.

    One-time passes: time until the guest's access ends (stacked passes
    included) divided by the plan's length. Subscriptions: time until the
    next renewal divided by the billing period."""
    t = db.now()
    if p["kind"] == "subscription":
        grace = db.get_int("subscription_grace_hours", 24) * 3600
        remaining = int(p["ends_at"]) - grace - t
        term = INTERVAL_SECONDS.get(plan["interval"], 30 * 86400) * max(int(plan["interval_count"] or 1), 1)
    else:
        remaining = (access_until(user_id) or t) - t
        if plan["duration_mode"] == "season":
            term = max(int(p["ends_at"]) - int(p["starts_at"]), 3600)
        else:
            term = max(int(plan["duration_hours"]) * 3600, 3600)
    return max(remaining, 0) / term


def quote_later(user_id: int):
    """What adding slots right now would cost. Returns None if not offered,
    else {plan, pass, held, max_more, options: [(n, cents, full_cents)]}."""
    p = governing_pass(user_id)
    if not p or not p["plan_id"]:
        return None
    plan = db.one("SELECT * FROM plans WHERE id=?", (p["plan_id"],))
    if not enabled(plan):
        return None
    h = held(user_id)
    max_more = int(plan["max_extra_devices"]) - h
    if max_more <= 0:
        return {"plan": plan, "pass": p, "held": h, "max_more": 0, "options": []}
    frac = fraction_left(user_id, p, plan)
    opts = []
    for n in range(1, max_more + 1):
        full = full_price(plan, h, n)
        cents = max(round(full * frac), STRIPE_MIN_CENTS)
        opts.append((n, cents, full))
    return {"plan": plan, "pass": p, "held": h, "max_more": max_more, "options": opts,
            "recurring": p["kind"] == "subscription"}
