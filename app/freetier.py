"""Free basic tier: an always-available, slow connection for basic browsing,
with the paid plans offered as the upgrade.

A guest taps "Use free basic Wi-Fi" on the sign-in page (no account, no
card). The device goes into the normal allow-list, so it gets internet, and
also into a "free" list on the router that has a low speed limit. Optionally
it's limited to a number of minutes per device per day.

The router does the slowing down (see the network guides). Paid access
always wins: a device with a pass, an always-online entry or a checkout in
progress is never put in the slow list.
"""
from datetime import datetime

from . import db
from .util import tz

SETTINGS = {
    "free_enabled": "0",
    "free_alias": "free_clients",
    "free_minutes": "0",          # per device per day; 0 = no limit
    "free_title": "Free basic Wi-Fi",
    "free_text": "Slow, but fine for messages, email and simple browsing. Want full speed? Pick a pass below.",
    "free_speed_label": "",       # shown to guests, e.g. "about 2 Mbps"
}


def get(key: str) -> str:
    v = db.get(key)
    return v if v != "" or key == "free_speed_label" else SETTINGS.get(key, "")


def enabled() -> bool:
    return db.get("free_enabled") == "1"


def alias() -> str:
    return get("free_alias") or "free_clients"


def minutes() -> int:
    try:
        return max(0, int(get("free_minutes") or 0))
    except ValueError:
        return 0


def today() -> str:
    return datetime.fromtimestamp(db.now(), tz()).strftime("%Y-%m-%d")


def _row(mac):
    return db.one("SELECT * FROM free_devices WHERE mac=?", (mac,)) if mac else None


def status(mac: str | None) -> dict:
    """{'on': connected now, 'until': end time or None (no limit),
        'used_up': today's free time is used up}"""
    r = _row(mac)
    t = db.now()
    if not r or not enabled():
        return {"on": False, "until": None, "used_up": False}
    if r["day"] != today() and minutes():
        return {"on": False, "until": None, "used_up": False}   # a new day: allowed again
    if r["expires_at"] and r["expires_at"] <= t:
        return {"on": False, "until": None, "used_up": bool(minutes())}
    return {"on": True, "until": r["expires_at"], "used_up": False}


def claim(mac: str, ip: str) -> str:
    """'ok', 'off' (tier disabled) or 'used' (today's minutes are used up)."""
    if not enabled():
        return "off"
    st = status(mac)
    if st["on"]:
        db.run("UPDATE free_devices SET ip=? WHERE mac=?", (ip, mac))
        return "ok"
    if st["used_up"]:
        return "used"
    t = db.now()
    exp = t + minutes() * 60 if minutes() else None
    db.run("INSERT INTO free_devices (mac, ip, day, started_at, expires_at) VALUES (?,?,?,?,?) "
           "ON CONFLICT(mac) DO UPDATE SET ip=excluded.ip, day=excluded.day, "
           "started_at=excluded.started_at, expires_at=excluded.expires_at",
           (mac, ip, today(), t, exp))
    db.log("free_claim", mac)
    return "ok"


def stop(mac: str):
    """The guest is done (or the owner removed them): end it for today."""
    db.run("UPDATE free_devices SET expires_at=? WHERE mac=?", (db.now(), mac))


def active_devices():
    """Rows for devices on the free tier right now."""
    if not enabled():
        return []
    t = db.now()
    rows = db.q("SELECT * FROM free_devices WHERE expires_at IS NULL OR expires_at>? ORDER BY started_at DESC", (t,))
    if minutes():   # with a daily limit, yesterday's unlimited rows don't count
        rows = [r for r in rows if r["day"] == today()]
    return rows


def reset():
    db.run("DELETE FROM free_devices")
