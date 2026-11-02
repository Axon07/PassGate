"""Free-access promotions: e.g. a free long weekend so people can try the
Wi-Fi. During the promotion the sign-in page offers a one-tap "Connect
free" button (no account, no card). When it ends, those devices go back to
the sign-in page, which thanks them and shows the paid plans."""
from datetime import datetime

from . import db
from .util import tz

SETTINGS = {
    "promo_enabled": "0",
    "promo_start": "",          # local time, YYYY-MM-DDTHH:MM
    "promo_end": "",
    "promo_title": "Free Wi-Fi this weekend!",
    "promo_text": "Try it on us. If you like it, keep it going with a pass or a monthly plan below.",
    "promo_after_text": "Thanks for trying the free Wi-Fi! Pick a pass or a monthly plan below to stay connected.",
    "promo_after_days": "14",   # show the thank-you note this long after it ends
}


def get(key: str) -> str:
    v = db.get(key)
    return v if v != "" or key in ("promo_start", "promo_end") else SETTINGS.get(key, "")


def _ts(local: str) -> int | None:
    try:
        return int(datetime.strptime(local, "%Y-%m-%dT%H:%M").replace(tzinfo=tz()).timestamp())
    except (ValueError, TypeError):
        return None


def window() -> tuple[int, int] | None:
    """(start, end) timestamps of the configured promotion, if valid."""
    if db.get("promo_enabled") != "1":
        return None
    s, e = _ts(db.get("promo_start")), _ts(db.get("promo_end"))
    if not s or not e or e <= s:
        return None
    return s, e


def active() -> tuple[int, int] | None:
    w = window()
    return w if w and w[0] <= db.now() < w[1] else None


def claim(mac: str, ip: str) -> bool:
    if not active():
        return False
    db.run("INSERT INTO promo_devices (mac, ip, claimed_at) VALUES (?,?,?) "
           "ON CONFLICT(mac) DO UPDATE SET ip=excluded.ip", (mac, ip, db.now()))
    db.log("promo_claim", mac)
    return True


def claimed(mac: str | None) -> bool:
    return bool(mac) and db.one("SELECT 1 FROM promo_devices WHERE mac=?", (mac,)) is not None


def show_thanks(mac: str | None) -> bool:
    """True for a device that used the promotion that ended recently."""
    w = window()
    if not w or not claimed(mac) or db.now() < w[1]:
        return False
    days = int(get("promo_after_days") or 0)
    return db.now() < w[1] + days * 86400


def devices():
    return db.q("SELECT * FROM promo_devices ORDER BY claimed_at DESC")


def reset():
    db.run("DELETE FROM promo_devices")
