"""Vouchers: printable codes for access you hand out yourself (a comp for a
neighbour, a night's Wi-Fi with a campsite booking, a prize, a pre-paid card
sold at the store).

The owner makes a batch: how many codes, how long each lasts, how many
devices each covers, an optional "use by" date and speed tier. A guest
types the code on the sign-in page (the same box as join codes). The clock
starts at first use; more devices can join with the same code until it's
full. There's no account, card or password involved.

Under the hood each redeemed voucher gets its own hidden account with one
pass, so device handling, sync and speed tiers work exactly as for paid
guests. Those accounts can't be logged in to and don't appear in the guest
list.
"""
import csv
import io
import secrets
from datetime import datetime, timedelta

from . import access, db
from .util import tz

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L
CODE_LEN = 8                                        # shown as ABCD-EFGH
MAX_BATCH = 500


def normalise(code: str) -> str:
    raw = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    return f"{raw[:4]}-{raw[4:]}" if len(raw) == CODE_LEN else raw


def looks_like_voucher(code: str) -> bool:
    return len("".join(ch for ch in (code or "") if ch.isalnum())) == CODE_LEN


def use_by_ts(date_str: str) -> int | None:
    """'YYYY-MM-DD' -> 23:59:59 that day, local time."""
    try:
        d = datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(tzinfo=tz())
    except (ValueError, AttributeError):
        return None
    return int((d + timedelta(days=1) - timedelta(seconds=1)).timestamp())


def describe_minutes(m: int) -> str:
    if m % 1440 == 0:
        d = m // 1440
        return f"{d} day{'s' if d != 1 else ''}"
    if m % 60 == 0:
        h = m // 60
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{m} minutes"


# ---- making them ---------------------------------------------------------------

def create_batch(name: str, count: int, minutes: int, max_devices: int = 1, use_by: int | None = None,
                 speed_tier: str = "", note: str = "") -> int:
    count = max(1, min(MAX_BATCH, int(count)))
    minutes = max(1, min(366 * 1440, int(minutes)))
    max_devices = max(1, min(20, int(max_devices)))
    t = db.now()
    bid = db.run("INSERT INTO voucher_batches (name, duration_minutes, max_devices, use_by, speed_tier, note, "
                 "created_at) VALUES (?,?,?,?,?,?,?)",
                 ((name or "Vouchers").strip()[:60], minutes, max_devices, use_by, speed_tier or "",
                  (note or "").strip()[:200], t))
    made = 0
    while made < count:
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        code = f"{raw[:4]}-{raw[4:]}"
        if db.one("SELECT id FROM vouchers WHERE code=?", (code,)):
            continue
        db.run("INSERT INTO vouchers (batch_id, code, created_at) VALUES (?,?,?)", (bid, code, t))
        made += 1
    db.log("vouchers_created", f"batch={bid} count={count} {describe_minutes(minutes)}")
    return bid


# ---- state ---------------------------------------------------------------------

def state(v, batch=None) -> str:
    """unused | active | used | expired (never used, past its use-by date) | revoked"""
    batch = batch or db.one("SELECT * FROM voucher_batches WHERE id=?", (v["batch_id"],))
    if v["revoked"]:
        return "revoked"
    if v["user_id"]:
        until = access.access_until(v["user_id"])
        return "active" if until else "used"
    if batch["use_by"] and batch["use_by"] < db.now():
        return "expired"
    return "unused"


def batches():
    out = []
    for b in db.q("SELECT * FROM voucher_batches ORDER BY created_at DESC"):
        counts = {"unused": 0, "active": 0, "used": 0, "expired": 0, "revoked": 0}
        for v in db.q("SELECT * FROM vouchers WHERE batch_id=?", (b["id"],)):
            counts[state(v, b)] += 1
        out.append({"b": b, "counts": counts, "total": sum(counts.values())})
    return out


def batch_codes(batch_id: int):
    b = db.one("SELECT * FROM voucher_batches WHERE id=?", (batch_id,))
    if not b:
        return None, []
    rows = []
    for v in db.q("SELECT * FROM vouchers WHERE batch_id=? ORDER BY id", (batch_id,)):
        until = access.access_until(v["user_id"]) if v["user_id"] else None
        ndev = db.one("SELECT COUNT(*) n FROM devices WHERE user_id=?", (v["user_id"],))["n"] if v["user_id"] else 0
        rows.append({"v": v, "state": state(v, b), "until": until, "devices": ndev})
    return b, rows


def owner_voucher(user_id: int):
    """The voucher behind a hidden voucher account, with its batch."""
    v = db.one("SELECT v.*, b.name AS batch_name, b.max_devices, b.duration_minutes FROM vouchers v "
               "JOIN voucher_batches b ON b.id=v.batch_id WHERE v.user_id=?", (user_id,))
    return v


# ---- using them ----------------------------------------------------------------

def redeem(code: str, mac: str, ip: str, client: str = ""):
    """Connect a device with a voucher. Returns (ok, result): the hidden
    account's user id, or a reason: slow_down, bad_code, expired, used,
    revoked, limit."""
    t = db.now()
    tries = [x for x in access._failed_joins.get(client, []) if x > t - 3600]
    access._failed_joins[client] = tries
    if len(tries) >= 10:
        return False, "slow_down"
    v = db.one("SELECT * FROM vouchers WHERE code=?", (normalise(code),))
    if not v:
        tries.append(t)
        return False, "bad_code"
    b = db.one("SELECT * FROM voucher_batches WHERE id=?", (v["batch_id"],))
    st = state(v, b)
    if st in ("expired", "used", "revoked"):
        return False, st
    uid = v["user_id"]
    if not uid:   # first use: the clock starts now
        uid = db.run("INSERT INTO users (email, pw_hash, created_at, voucher_id) VALUES (?,?,?,?)",
                     (f"voucher-{v['code'].lower()}@vouchers.invalid", "!", t, v["id"]))
        db.run("INSERT INTO passes (user_id, plan_name, kind, max_devices, status, source, starts_at, ends_at, "
               "amount_cents, speed_tier, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (uid, b["name"], "voucher", b["max_devices"], "active", "voucher", t,
                t + b["duration_minutes"] * 60, 0, b["speed_tier"] or "", t))
        db.run("UPDATE vouchers SET user_id=?, redeemed_at=? WHERE id=?", (uid, t, v["id"]))
        db.log("voucher_used", f"code={v['code']} batch={b['id']}")
    ok, why = access.add_device(uid, mac, ip, label="Voucher " + v["code"], source="voucher", skip_cooldown=True)
    if not ok:
        return False, why
    return True, uid


def revoke(voucher_id: int):
    v = db.one("SELECT * FROM vouchers WHERE id=?", (voucher_id,))
    if not v:
        return
    db.run("UPDATE vouchers SET revoked=1 WHERE id=?", (voucher_id,))
    if v["user_id"]:
        db.run("UPDATE passes SET status='ended', ends_at=? WHERE user_id=? AND status='active'",
               (db.now(), v["user_id"]))
    db.log("voucher_revoked", v["code"])


def delete_unused(batch_id: int) -> int:
    rows = [r for r in db.q("SELECT * FROM vouchers WHERE batch_id=? AND user_id IS NULL", (batch_id,))]
    for r in rows:
        db.run("DELETE FROM vouchers WHERE id=?", (r["id"],))
    if not db.one("SELECT id FROM vouchers WHERE batch_id=?", (batch_id,)):
        db.run("DELETE FROM voucher_batches WHERE id=?", (batch_id,))
    return len(rows)


def to_csv(batch_id: int) -> str:
    b, rows = batch_codes(batch_id)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["code", "status", "batch", "lasts", "devices", "use_by", "first_used", "active_until"])
    fmt = lambda ts: datetime.fromtimestamp(ts, tz()).strftime("%Y-%m-%d %H:%M") if ts else ""  # noqa: E731
    for r in rows:
        w.writerow([r["v"]["code"], r["state"], b["name"], describe_minutes(b["duration_minutes"]),
                    b["max_devices"], fmt(b["use_by"]), fmt(r["v"]["redeemed_at"]), fmt(r["until"])])
    return out.getvalue()
