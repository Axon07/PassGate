"""Who is allowed online right now, and keeping the router in step with that."""
import logging
import threading

from . import db
from .gateway import base_alias, get_driver, local_arp, mac_for_ip, speed_tiers

log = logging.getLogger("passgate.access")
_sync_lock = threading.Lock()


# ---- passes ---------------------------------------------------------------

def _active_where():
    return "status='active' AND starts_at<=? AND ends_at>?", (db.now(), db.now())


def active_passes(user_id: int):
    w, p = _active_where()
    return db.q(f"SELECT * FROM passes WHERE user_id=? AND {w} ORDER BY ends_at DESC", (user_id, *p))


def access_until(user_id: int) -> int | None:
    w, p = _active_where()
    r = db.one(f"SELECT MAX(ends_at) AS e FROM passes WHERE user_id=? AND {w}", (user_id, *p))
    return r["e"] if r and r["e"] else None


def device_limit(user_id: int) -> int:
    """Devices allowed right now = included devices + paid extra slots.
    Included devices come from the per-guest override if the owner set one,
    otherwise the best active pass. Zero without an active pass."""
    w, p = _active_where()
    r = db.one(f"SELECT MAX(max_devices) AS m, COALESCE(SUM(extra_devices),0) AS x "
               f"FROM passes WHERE user_id=? AND {w}", (user_id, *p))
    if not r or not r["m"]:
        return 0
    u = db.one("SELECT device_limit_override AS o FROM users WHERE id=?", (user_id,))
    base = int(u["o"]) if u and u["o"] else int(r["m"])
    return base + int(r["x"])


def has_access(user_id: int) -> bool:
    u = db.one("SELECT disabled FROM users WHERE id=?", (user_id,))
    return bool(u) and not u["disabled"] and access_until(user_id) is not None


def stacking_start(user_id: int) -> int:
    """A new one-time pass starts when the current one ends, so buying a day
    pass on top of a weekend pass adds time instead of wasting it."""
    r = db.one("SELECT MAX(ends_at) AS e FROM passes WHERE user_id=? AND status='active' "
               "AND kind='one_time' AND ends_at>?", (user_id, db.now()))
    return max(db.now(), int(r["e"])) if r and r["e"] else db.now()


def user_tier_alias(user_id: int) -> str | None:
    """Speed-tier alias for this guest, from their active passes. If they
    hold passes in several tiers, the one listed last in Settings wins."""
    tiers = speed_tiers()
    if not tiers:
        return None
    held = {p["speed_tier"] for p in active_passes(user_id) if p["speed_tier"]}
    best = None
    for name, alias in tiers:
        if name in held:
            best = alias
    return best


# ---- devices --------------------------------------------------------------

def devices(user_id: int):
    return db.q("SELECT * FROM devices WHERE user_id=? ORDER BY added_at", (user_id,))


def cooldown_left(user_id: int) -> int:
    """Seconds until this guest may swap devices again (0 = now)."""
    mins = db.get_int("swap_cooldown_minutes", 60)
    u = db.one("SELECT last_swap_at FROM users WHERE id=?", (user_id,))
    if mins <= 0 or not u or not u["last_swap_at"]:
        return 0
    return max(0, int(u["last_swap_at"]) + mins * 60 - db.now())


def add_device(user_id: int, mac: str, ip: str = "", label: str = "", replace_id: int | None = None,
               source: str = "auto", by_admin: bool = False, skip_cooldown: bool = False):
    """Attach a device to an account. Returns (ok, code). Codes:
    already, added, moved      -> ok
    noaccess, limit, cooldown, taken -> not added
    source='auto' means the device itself is asking (we read its MAC off the
    network); 'manual' means someone typed the MAC in."""
    t = db.now()
    existing = db.one("SELECT * FROM devices WHERE mac=?", (mac,))
    if existing and existing["user_id"] == user_id:
        if ip:
            db.run("UPDATE devices SET last_ip=?, last_seen=? WHERE id=?", (ip, t, existing["id"]))
        return True, "already"
    if existing and source == "manual" and not by_admin:
        # A typed-in MAC could be anyone's. Only the device itself (logging
        # in on it) or the owner can move it off another account.
        return False, "taken"
    limit = device_limit(user_id)
    if limit <= 0:
        return False, "noaccess"
    enforce_cooldown = not (by_admin or skip_cooldown) and cooldown_left(user_id) > 0
    if replace_id:
        if enforce_cooldown:
            return False, "cooldown"
        db.run("DELETE FROM devices WHERE id=? AND user_id=?", (replace_id, user_id))
        if not (by_admin or skip_cooldown):
            db.run("UPDATE users SET last_swap_at=? WHERE id=?", (t, user_id))
    mine = devices(user_id)
    if len(mine) >= limit:
        return False, "limit"
    if enforce_cooldown and not replace_id:
        # Filling a slot they just emptied counts as a swap too.
        return False, "cooldown"
    code = "added"
    if existing:  # this device was on another account (e.g. a kid's phone)
        db.run("DELETE FROM devices WHERE id=?", (existing["id"],))
        db.log("device_moved", f"mac={mac} from user={existing['user_id']} to user={user_id}")
        code = "moved"
    db.run("INSERT INTO devices (user_id, mac, last_ip, label, added_at, last_seen, source) "
           "VALUES (?,?,?,?,?,?,?)", (user_id, mac, ip, (label or "")[:40], t, t if ip else None, source))
    db.log("device_added", f"user={user_id} mac={mac} ({source}{', admin' if by_admin else ''})")
    return True, code


def remove_device(user_id: int, device_id: int, by_admin: bool = False):
    if db.one("SELECT id FROM devices WHERE id=? AND user_id=?", (device_id, user_id)):
        db.run("DELETE FROM devices WHERE id=? AND user_id=?", (device_id, user_id))
        if not by_admin:
            db.run("UPDATE users SET last_swap_at=? WHERE id=?", (db.now(), user_id))


# ---- join codes (let a kid's device use a parent's account) -----------------

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L
_failed_joins: dict[str, list[int]] = {}


def create_invite(user_id: int, label: str = "", hours: int = 24) -> str:
    import secrets
    db.run("DELETE FROM invites WHERE expires_at<?", (db.now() - 7 * 86400,))
    while True:
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        code = f"{raw[:3]}-{raw[3:]}"
        if not db.one("SELECT id FROM invites WHERE code=?", (code,)):
            break
    db.run("INSERT INTO invites (code, user_id, label, expires_at, created_at) VALUES (?,?,?,?,?)",
           (code, user_id, (label or "")[:40], db.now() + hours * 3600, db.now()))
    db.log("invite_created", f"user={user_id} code={code}")
    return code


def open_invites(user_id: int):
    return db.q("SELECT * FROM invites WHERE user_id=? AND used_at IS NULL AND expires_at>? "
                "ORDER BY created_at DESC", (user_id, db.now()))


def cancel_invite(user_id: int, invite_id: int):
    db.run("DELETE FROM invites WHERE id=? AND user_id=? AND used_at IS NULL", (invite_id, user_id))


def redeem_invite(code: str, mac: str, ip: str, client: str = ""):
    """Join a device to the account that made the code. Returns (ok, result)
    where result is the owner's user id on success, else a reason:
    slow_down, bad_code, noaccess, limit."""
    t = db.now()
    tries = [x for x in _failed_joins.get(client, []) if x > t - 3600]
    _failed_joins[client] = tries
    if len(tries) >= 10:
        return False, "slow_down"
    norm = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    norm = f"{norm[:3]}-{norm[3:]}" if len(norm) == 6 else norm
    inv = db.one("SELECT * FROM invites WHERE code=? AND used_at IS NULL AND expires_at>?", (norm, t))
    if not inv:
        tries.append(t)
        return False, "bad_code"
    owner = inv["user_id"]
    if not has_access(owner):
        return False, "noaccess"
    ok, why = add_device(owner, mac, ip, label=inv["label"] or "Joined with code", source="invite",
                         skip_cooldown=True)
    if not ok:
        return False, why
    db.run("UPDATE invites SET used_at=?, used_mac=? WHERE id=?", (t, mac, inv["id"]))
    db.log("invite_used", f"user={owner} code={norm} mac={mac}")
    return True, owner


# ---- always-online list ----------------------------------------------------

def bypass_list():
    return db.q("SELECT * FROM bypass ORDER BY label, mac")


def add_bypass(mac: str, label: str = ""):
    db.run("INSERT INTO bypass (mac, label, created_at) VALUES (?,?,?) "
           "ON CONFLICT(mac) DO UPDATE SET label=excluded.label", (mac, (label or "")[:60], db.now()))
    db.log("bypass_added", f"{mac} {label}")


def remove_bypass(bypass_id: int):
    db.run("DELETE FROM bypass WHERE id=?", (bypass_id,))


def grant_checkout_access(mac: str, ip: str) -> bool:
    """Short internet window so the guest can load Stripe's payment page.
    Limited per device per day so it can't be used as free Wi-Fi."""
    per_day = db.get_int("checkout_grants_per_day", 4)
    used = db.one("SELECT COUNT(*) AS n FROM temp_grants WHERE mac=? AND created_at>?",
                  (mac, db.now() - 86400))["n"]
    if used >= per_day:
        return False
    mins = db.get_int("checkout_grace_minutes", 15)
    db.run("INSERT INTO temp_grants (mac, ip, expires_at, created_at) VALUES (?,?,?,?)",
           (mac, ip, db.now() + mins * 60, db.now()))
    sync()
    return True


# ---- the sync loop ----------------------------------------------------------

def wanted() -> dict[str, set[str]]:
    """{alias: IPs that belong in it right now}. The base alias = who gets
    internet; tier aliases = which speed limit applies."""
    t = db.now()
    base = base_alias()
    out: dict[str, set[str]] = {base: set()}
    for _, alias in speed_tiers():
        out.setdefault(alias, set())

    try:
        arp = get_driver().arp_table()
    except Exception as e:
        log.warning("router ARP unavailable: %s", e)
        arp = {}
    arp.update(local_arp())
    mac_to_ip = {mac: ip for ip, mac in arp.items()}

    def ip_for(mac, last_ip):
        ip = mac_to_ip.get(mac)
        if ip:
            return ip
        if mac.startswith("ip-"):          # dry-run identity
            return mac[3:]
        if not arp and last_ip:
            # Router's ARP table unreadable: fall back to the last known IP
            # rather than kicking everyone offline.
            return last_ip
        return None

    rows = db.q("""
        SELECT d.*, u.id AS uid FROM devices d JOIN users u ON u.id=d.user_id
        WHERE u.disabled=0 AND EXISTS (
            SELECT 1 FROM passes p WHERE p.user_id=u.id AND p.status='active'
            AND p.starts_at<=? AND p.ends_at>?)
        ORDER BY d.added_at DESC""", (t, t))
    counted: dict[int, int] = {}
    limits: dict[int, int] = {}
    tiers: dict[int, str | None] = {}
    for d in rows:
        uid = d["uid"]
        if uid not in limits:
            limits[uid], tiers[uid] = device_limit(uid), user_tier_alias(uid)
        counted[uid] = counted.get(uid, 0) + 1
        if counted[uid] > limits[uid]:      # never more than the pass covers (newest win)
            continue
        ip = ip_for(d["mac"], d["last_ip"])
        if not ip:
            continue
        if ip != d["last_ip"]:
            db.run("UPDATE devices SET last_ip=?, last_seen=? WHERE id=?", (ip, t, d["id"]))
        out[base].add(ip)
        if tiers[uid]:
            out[tiers[uid]].add(ip)

    for b in bypass_list():
        ip = ip_for(b["mac"], None)
        if ip:
            out[base].add(ip)
    for g in db.q("SELECT * FROM temp_grants WHERE expires_at>?", (t,)):
        out[base].add(mac_to_ip.get(g["mac"], g["ip"]))
    from . import freetier, promo
    if promo.active():  # free-access promotion: everyone who tapped "Connect free"
        for p in promo.devices():
            ip = ip_for(p["mac"], p["ip"] if not arp else None)
            if ip:
                out[base].add(ip)
    if freetier.enabled():
        # Free basic tier: online, but also in the slow list. Anyone already
        # online another way (paid, always-online, paying now) keeps full speed.
        slow = out.setdefault(freetier.alias(), set())
        for f in freetier.active_devices():
            ip = ip_for(f["mac"], f["ip"] if not arp else None)
            if ip and ip not in out[base]:
                out[base].add(ip)
                slow.add(ip)
    old = db.get("free_cleanup_alias")
    if old and old not in out:
        out[old] = set()    # tier turned off or renamed: empty the old slow list once
    return out


def wanted_ips() -> set[str]:
    return wanted()[base_alias()]


def sync() -> dict:
    """Make the router's aliases match what PassGate says. Safe to call often."""
    with _sync_lock:
        drv = get_driver()
        base = base_alias()
        want = wanted()
        result = {"added": [], "removed": [], "online": len(want[base])}
        for alias, ips in want.items():
            try:
                have = drv.list_allowed(alias)
            except Exception as e:
                log.error("can't read alias %s from router: %s", alias, e)
                if alias == base:
                    result["error"] = str(e)
                continue
            for ip in ips - have:
                try:
                    drv.allow(alias, ip)
                except Exception as e:
                    log.error("allow %s in %s failed: %s", ip, alias, e)
            for ip in have - ips:
                try:
                    # only cut connections when internet access itself ends
                    drv.revoke(alias, ip, kill_states=(alias == base))
                except Exception as e:
                    log.error("revoke %s from %s failed: %s", ip, alias, e)
            if alias == base:
                result["added"], result["removed"] = sorted(ips - have), sorted(have - ips)
            if alias == db.get("free_cleanup_alias") and not ips:
                db.set("free_cleanup_alias", "")
        # housekeeping
        t = db.now()
        db.run("UPDATE passes SET status='ended' WHERE status='active' AND ends_at<=?", (t,))
        db.run("DELETE FROM temp_grants WHERE created_at<?", (t - 2 * 86400,))
        db.run("DELETE FROM passes WHERE status='pending' AND created_at<?", (t - 2 * 86400,))
        if result["added"] or result["removed"]:
            log.info("sync: +%d -%d (online now: %d)", len(result["added"]), len(result["removed"]),
                     result["online"])
        return result


def identify(ip: str):
    """(mac, user_id or None) for the device making this request."""
    mac = mac_for_ip(ip)
    if not mac:
        return None, None
    d = db.one("SELECT user_id FROM devices WHERE mac=?", (mac,))
    return mac, (d["user_id"] if d else None)
