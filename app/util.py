"""Small helpers: password hashing, time formatting, money, networks."""
import hashlib
import hmac
import ipaddress
import os
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db


# ---- passwords (stdlib scrypt, no extra dependency) -----------------------

def hash_pw(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        test = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(test.hex(), h)
    except Exception:
        return False


def secret_key() -> str:
    """Session-signing key, generated once and kept in the data folder."""
    path = os.path.join(db.DATA_DIR, "secret.key")
    if not os.path.exists(path):
        os.makedirs(db.DATA_DIR, exist_ok=True)
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as f:
        return f.read().strip()


# ---- time -----------------------------------------------------------------

def tz() -> ZoneInfo:
    try:
        return ZoneInfo(db.get("timezone") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def fmt_ts(ts) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), tz()).strftime("%a %b %-d, %-I:%M %p")


def season_end_ts() -> int | None:
    """End of the configured season (23:59:59 local), or None if unset/past."""
    s = (db.get("season_end") or "").strip()
    if not s:
        return None
    try:
        d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=tz())
    except ValueError:
        return None
    ts = int((d + timedelta(days=1) - timedelta(seconds=1)).timestamp())
    return ts if ts > db.now() else None


def remaining(ts) -> str:
    if not ts:
        return ""
    s = int(ts) - db.now()
    if s <= 0:
        return "expired"
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h left"
    if h:
        return f"{h}h {m}m left"
    return f"{m}m left"


# ---- money ----------------------------------------------------------------

SYMBOLS = {"cad": "$", "usd": "$", "aud": "$", "nzd": "$", "eur": "€", "gbp": "£"}


def money(cents, currency=None) -> str:
    cur = (currency or db.get("currency") or "usd").lower()
    sym = SYMBOLS.get(cur, "")
    amount = f"{int(cents) / 100:,.2f}"
    if amount.endswith(".00"):
        amount = amount[:-3]
    return f"{sym}{amount}" if sym else f"{amount} {cur.upper()}"


# ---- plan wording ---------------------------------------------------------

def describe_term(plan) -> str:
    if plan["kind"] == "subscription":
        n, unit = int(plan["interval_count"]), plan["interval"]
        every = unit if n == 1 else f"{n} {unit}s"
        txt = f"billed every {every}"
        se = season_end_ts()
        if plan["stop_at_season_end"] and se:
            txt += f", stops automatically {datetime.fromtimestamp(se, tz()).strftime('%b %-d')}"
        return txt
    if plan["duration_mode"] == "season":
        se = season_end_ts()
        return "until end of season" + (f" ({datetime.fromtimestamp(se, tz()).strftime('%b %-d')})" if se else "")
    h = int(plan["duration_hours"])
    if h % 24 == 0:
        d = h // 24
        return f"{d} day" + ("s" if d != 1 else "") + " of access"
    return f"{h} hours of access"


# ---- networks -------------------------------------------------------------

def ip_in(ip: str, cidrs: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for c in [x.strip() for x in (cidrs or "").split(",") if x.strip()]:
        try:
            if addr in ipaddress.ip_network(c, strict=False):
                return True
        except ValueError:
            continue
    return False


def now_utc():
    return datetime.now(timezone.utc)
