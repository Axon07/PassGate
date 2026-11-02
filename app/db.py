"""SQLite storage. One file in DATA_DIR, no external database needed."""
import os
import sqlite3
import threading
import time

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "passgate.db")

_local = threading.local()
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    price_cents INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'one_time',          -- one_time | subscription
    duration_mode TEXT NOT NULL DEFAULT 'hours',    -- hours | season (one_time only)
    duration_hours INTEGER NOT NULL DEFAULT 24,
    interval TEXT NOT NULL DEFAULT 'month',         -- day | week | month | year (subscription only)
    interval_count INTEGER NOT NULL DEFAULT 1,
    stop_at_season_end INTEGER NOT NULL DEFAULT 1,  -- subscription: auto-cancel at season end
    max_devices INTEGER NOT NULL DEFAULT 2,
    active INTEGER NOT NULL DEFAULT 1,
    sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    pw_hash TEXT NOT NULL,
    stripe_customer_id TEXT,
    disabled INTEGER NOT NULL DEFAULT 0,
    notes TEXT DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS passes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    plan_id INTEGER,
    plan_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    max_devices INTEGER NOT NULL,
    status TEXT NOT NULL,              -- pending | active | ended
    source TEXT NOT NULL,              -- stripe | admin
    starts_at INTEGER,
    ends_at INTEGER,
    amount_cents INTEGER DEFAULT 0,
    stripe_session_id TEXT UNIQUE,
    stripe_subscription_id TEXT,
    buy_mac TEXT,
    buy_ip TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    mac TEXT NOT NULL UNIQUE,
    last_ip TEXT,
    label TEXT DEFAULT '',
    added_at INTEGER NOT NULL,
    last_seen INTEGER
);
CREATE TABLE IF NOT EXISTS temp_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mac TEXT NOT NULL,
    ip TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS bypass (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mac TEXT NOT NULL UNIQUE,
    label TEXT DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    label TEXT DEFAULT '',
    expires_at INTEGER NOT NULL,
    used_at INTEGER,
    used_mac TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_passes_user ON passes(user_id);
CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);
CREATE INDEX IF NOT EXISTS idx_grants_exp ON temp_grants(expires_at);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


# Columns added after the first release. init() adds any that are missing,
# so upgrading is just: pull the new image and restart.
MIGRATIONS = [
    ("plans", "speed_tier", "TEXT DEFAULT ''"),
    ("passes", "speed_tier", "TEXT DEFAULT ''"),
    ("users", "device_limit_override", "INTEGER"),
    ("users", "last_swap_at", "INTEGER"),
    ("devices", "source", "TEXT DEFAULT 'auto'"),   # auto | manual (typed MAC) | invite (join code)
    # paid extra device slots
    ("plans", "extra_device_cents", "INTEGER DEFAULT 0"),      # 0 = extras not sold on this plan
    ("plans", "max_extra_devices", "INTEGER DEFAULT 3"),
    ("plans", "extra_discount_mode", "TEXT DEFAULT 'none'"),   # none | percent | amount
    ("plans", "extra_discount_value", "INTEGER DEFAULT 0"),    # % or cents off each further slot
    ("plans", "extra_min_cents", "INTEGER DEFAULT 0"),         # discount never goes below this
    ("passes", "extra_devices", "INTEGER DEFAULT 0"),
    ("passes", "parent_pass_id", "INTEGER"),
]


def init():
    with _lock:
        conn().executescript(SCHEMA)
        for table, col, decl in MIGRATIONS:
            cols = {r["name"] for r in conn().execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                conn().execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def q(sql, params=()):
    with _lock:
        return conn().execute(sql, params).fetchall()


def one(sql, params=()):
    with _lock:
        return conn().execute(sql, params).fetchone()


def run(sql, params=()):
    with _lock:
        cur = conn().execute(sql, params)
        return cur.lastrowid


def now() -> int:
    return int(time.time())


def log(kind: str, detail: str = ""):
    run("INSERT INTO events (ts, kind, detail) VALUES (?,?,?)", (now(), kind, detail))
    # keep the log from growing forever
    run("DELETE FROM events WHERE id < (SELECT MAX(id) - 5000 FROM events)")


# ---- settings -------------------------------------------------------------

DEFAULTS = {
    "site_name": "Lake Wi-Fi",
    "welcome_text": "Get online at the lake. Pick a pass, pay with card, and you're connected.",
    "currency": "cad",
    "timezone": "America/Winnipeg",
    "season_end": "",                 # YYYY-MM-DD, local time end of day
    "portal_url": "",                 # e.g. http://wifi.lake.lan  (what guests are redirected to)
    "guest_subnet": "10.50.0.0/22",
    "admin_allowed_cidrs": "",        # blank = any address outside guest_subnet
    "stripe_secret_key": "",
    "stripe_webhook_secret": "",
    "gateway_driver": "dryrun",       # dryrun | opnsense
    "opnsense_url": "https://10.51.0.1",
    "opnsense_key": "",
    "opnsense_secret": "",
    "opnsense_alias": "paid_clients",
    "opnsense_verify_tls": "0",
    "checkout_grace_minutes": "15",   # temporary internet while the guest is on the Stripe page
    "checkout_grants_per_day": "4",
    "subscription_grace_hours": "24", # keep access this long after a renewal is due
    "swap_cooldown_minutes": "60",    # 0 = guests can swap devices any time
    "guest_manual_mac": "1",          # guests may type in a MAC for a TV/console
    "speed_tiers": "",                # one per line: Name = opnsense_alias  (blank = off)
    "support_text": "",
    "setup_complete": "0",
    "admin_pw_hash": "",
}


def get(key: str) -> str:
    r = one("SELECT value FROM settings WHERE key=?", (key,))
    if r is None or r["value"] is None:
        return DEFAULTS.get(key, "")
    return r["value"]


def get_int(key: str, fallback: int = 0) -> int:
    try:
        return int(get(key))
    except (TypeError, ValueError):
        return fallback


def set(key: str, value):  # noqa: A001 - simple settings API
    run("INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def all_settings() -> dict:
    out = dict(DEFAULTS)
    for r in q("SELECT key, value FROM settings"):
        out[r["key"]] = r["value"]
    return out
