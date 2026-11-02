"""End-to-end test of the guest and admin flows with Stripe mocked out and
the router in dry-run mode. Run with:  pytest -q"""
import os
import re
import tempfile
import time

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="passgate-test-")

import pytest  # noqa: E402
import stripe  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import access, admin, billing, db, portal  # noqa: E402
from app.gateway import DryRun  # noqa: E402
from app.util import secret_key  # noqa: E402

db.init()
SECRET = secret_key()
PORTAL = portal.build(SECRET)
ADMIN = admin.build(SECRET)


# ---- fake Stripe -------------------------------------------------------------

class FakeStripe:
    sessions: dict = {}
    subs: dict = {}
    n = 0

    @classmethod
    def create(cls, **kw):
        cls.n += 1
        sid = f"cs_test_{cls.n}"
        cls.sessions[sid] = {"id": sid, "url": f"https://checkout.stripe.com/pay/{sid}",
                             "mode": kw["mode"], "payment_status": "unpaid", "status": "open",
                             "customer": None, "subscription": None, "params": kw}
        return cls.sessions[sid]

    @classmethod
    def retrieve(cls, sid):
        return cls.sessions[sid]

    @classmethod
    def pay(cls, sid):
        s = cls.sessions[sid]
        s.update(payment_status="paid", status="complete", customer="cus_123")
        if s["mode"] == "subscription":
            sub = f"sub_{sid}"
            s["subscription"] = sub
            items = []
            for li in s["params"]["line_items"]:
                pd = li["price_data"]
                items.append({"current_period_end": int(time.time()) + 30 * 86400, "quantity": 1,
                              "price": {"unit_amount": pd["unit_amount"],
                                        "product": {"name": pd["product_data"]["name"],
                                                    "metadata": pd["product_data"].get("metadata", {})}}})
            cls.subs[sub] = {"id": sub, "status": "active", "items": {"data": items}}

    @classmethod
    def sub_retrieve(cls, sub_id, **kw):
        return cls.subs[sub_id]

    products: dict = {}

    @classmethod
    def product_create(cls, **kw):
        pid = f"prod_{len(cls.products) + 1}"
        cls.products[pid] = {"id": pid, **kw}
        return cls.products[pid]

    @classmethod
    def item_create(cls, subscription, **kw):
        prod = cls.products[kw["price_data"]["product"]]
        item = {"id": f"si_{len(cls.subs[subscription]['items']['data']) + 1}", "quantity": kw.get("quantity", 1),
                "price": {"unit_amount": kw["price_data"]["unit_amount"], "product": prod}, "kw": kw}
        cls.subs[subscription]["items"]["data"].append(item)
        return item

    @classmethod
    def sub_modify(cls, sub_id, **kw):
        cls.subs[sub_id].update(kw)
        return cls.subs[sub_id]


@pytest.fixture(autouse=True)
def fake_stripe(monkeypatch):
    monkeypatch.setattr(stripe.checkout.Session, "create", FakeStripe.create)
    monkeypatch.setattr(stripe.checkout.Session, "retrieve", FakeStripe.retrieve)
    monkeypatch.setattr(stripe.Subscription, "retrieve", FakeStripe.sub_retrieve)
    monkeypatch.setattr(stripe.Subscription, "modify", FakeStripe.sub_modify)
    monkeypatch.setattr(stripe.Product, "create", FakeStripe.product_create)
    monkeypatch.setattr(stripe.SubscriptionItem, "create", FakeStripe.item_create)


def guest(ip, mac):
    DryRun.fake_arp[ip] = mac
    return TestClient(PORTAL, client=(ip, 40000), follow_redirects=False)


def allowed(alias="paid_clients"):
    return DryRun.tables.get(alias, set())


def owner():
    return TestClient(ADMIN, client=("192.168.1.10", 40000), follow_redirects=False)


def csrf(resp):
    return re.search(r'name="csrf" value="([^"]+)"', resp.text).group(1)


# ---- tests -------------------------------------------------------------------

def test_01_portal_not_ready_before_setup():
    r = guest("10.50.0.20", "aa:aa:aa:aa:aa:01").get("/")
    assert r.status_code == 503


def test_02_admin_blocked_from_guest_network():
    c = TestClient(ADMIN, client=("10.50.0.99", 40000), follow_redirects=False)
    assert c.get("/setup").status_code == 403


def test_03_first_run_setup():
    c = owner()
    r = c.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    page = c.get("/setup")
    tok = csrf(page)
    r = c.post("/setup", data={"csrf": tok, "site_name": "Test Lake", "currency": "cad",
                               "timezone": "America/Toronto", "season_end": "",
                               "portal_url": "http://testserver", "guest_subnet": "10.50.0.0/22",
                               "stripe_secret_key": "sk_test_x", "admin_password": "hunter2hunter2",
                               "admin_password2": "hunter2hunter2"})
    assert r.status_code == 303
    plans = c.get("/plans")
    assert "Day Pass" in plans.text and "Weekend Pass" in plans.text and "Monthly" in plans.text
    assert len(db.q("SELECT * FROM plans")) == 3


def test_04_captive_redirect_for_foreign_hosts():
    c = guest("10.50.0.20", "aa:aa:aa:aa:aa:01")
    r = c.get("/hotspot-detect.html", headers={"host": "captive.apple.com"})
    assert r.status_code == 302 and r.headers["location"] == "http://testserver/"
    home = c.get("/")
    assert home.status_code == 200 and "Day Pass" in home.text


def test_05_buy_day_pass_end_to_end():
    c = guest("10.50.0.20", "aa:aa:aa:aa:aa:01")
    day = db.one("SELECT id FROM plans WHERE name='Day Pass'")["id"]
    r = c.post("/start", data={"plan_id": day, "email": "Guest@Example.com", "password": "secret1"})
    assert r.status_code == 303 and r.headers["location"].startswith("https://checkout.stripe.com/")
    # temporary access while paying
    assert "10.50.0.20" in allowed()
    sid = list(FakeStripe.sessions)[-1]
    assert FakeStripe.sessions[sid]["params"]["mode"] == "payment"
    assert FakeStripe.sessions[sid]["params"]["line_items"][0]["price_data"]["unit_amount"] == 1000
    FakeStripe.pay(sid)
    r = c.get(f"/paid?session_id={sid}")
    assert r.status_code == 303 and r.headers["location"] == "/account?msg=paid"
    acct = c.get("/account")
    assert "Online" in acct.text and "1 of 2" in acct.text
    uid = db.one("SELECT id FROM users WHERE email='guest@example.com'")["id"]
    until = access.access_until(uid)
    assert 23 * 3600 < until - time.time() <= 24 * 3600
    # expire the temp grant: device must still be allowed via its pass
    db.run("UPDATE temp_grants SET expires_at=0")
    access.sync()
    assert "10.50.0.20" in allowed()


def test_06_second_device_login_and_limit():
    laptop = guest("10.50.0.21", "aa:aa:aa:aa:aa:02")
    r = laptop.post("/login", data={"email": "guest@example.com", "password": "secret1"})
    assert r.headers["location"] == "/account?msg=online"
    assert "10.50.0.21" in allowed()
    # third device hits the 2-device limit and can swap one out
    tablet = guest("10.50.0.22", "aa:aa:aa:aa:aa:03")
    r = tablet.post("/login", data={"email": "guest@example.com", "password": "secret1"})
    assert r.headers["location"] == "/account?msg=limit"
    assert "10.50.0.22" not in allowed()
    page = tablet.get("/account?msg=limit")
    assert "Swap for this device" in page.text
    phone_dev = db.one("SELECT id FROM devices WHERE mac='aa:aa:aa:aa:aa:01'")["id"]
    r = tablet.post("/device/add", data={"replace_id": phone_dev})
    assert r.headers["location"] == "/account?msg=online"
    assert "10.50.0.22" in allowed() and "10.50.0.20" not in allowed()


def test_07_ip_change_follows_mac():
    # tablet gets a new DHCP lease; allow-list should follow the MAC
    DryRun.fake_arp.pop("10.50.0.22")
    DryRun.fake_arp["10.50.0.77"] = "aa:aa:aa:aa:aa:03"
    access.sync()
    assert "10.50.0.77" in allowed() and "10.50.0.22" not in allowed()


def test_08_stacking_passes():
    uid = db.one("SELECT id FROM users WHERE email='guest@example.com'")["id"]
    before = access.access_until(uid)
    c = guest("10.50.0.21", "aa:aa:aa:aa:aa:02")
    c.cookies.clear()
    c.post("/login", data={"email": "guest@example.com", "password": "secret1"})
    wk = db.one("SELECT id FROM plans WHERE name='Weekend Pass'")["id"]
    r = c.post("/buy", data={"plan_id": wk})
    sid = list(FakeStripe.sessions)[-1]
    FakeStripe.pay(sid)
    billing.reconcile()  # simulates the guest closing the browser: poller fulfils it
    after = access.access_until(uid)
    assert abs((after - before) - 72 * 3600) < 5


def test_09_subscription_and_cancellation():
    c = guest("10.50.0.30", "bb:bb:bb:bb:bb:01")
    monthly = db.one("SELECT id FROM plans WHERE name='Monthly'")["id"]
    db.set("season_end", time.strftime("%Y-%m-%d", time.localtime(time.time() + 60 * 86400)))
    c.post("/start", data={"plan_id": monthly, "email": "sub@example.com", "password": "secret1"})
    sid = list(FakeStripe.sessions)[-1]
    assert FakeStripe.sessions[sid]["params"]["mode"] == "subscription"
    assert FakeStripe.sessions[sid]["params"]["line_items"][0]["price_data"]["recurring"]["interval"] == "month"
    FakeStripe.pay(sid)
    c.get(f"/paid?session_id={sid}")
    sub = FakeStripe.subs[f"sub_{sid}"]
    assert sub.get("cancel_at")  # auto-stops at season end
    uid = db.one("SELECT id FROM users WHERE email='sub@example.com'")["id"]
    assert access.has_access(uid) and "10.50.0.30" in allowed()
    # Renewal pushes access forward
    sub["items"]["data"][0]["current_period_end"] = int(time.time()) + 61 * 86400
    billing.reconcile()
    assert access.access_until(uid) > time.time() + 60 * 86400
    # Customer cancels in Stripe -> access ends on next poll
    sub["status"] = "canceled"
    sub["ended_at"] = int(time.time()) - 1
    billing.reconcile()
    assert not access.has_access(uid)
    assert "10.50.0.30" not in allowed()


def test_10_unpaid_checkout_gives_no_lasting_access():
    c = guest("10.50.0.40", "cc:cc:cc:cc:cc:01")
    day = db.one("SELECT id FROM plans WHERE name='Day Pass'")["id"]
    c.post("/start", data={"plan_id": day, "email": "cheap@example.com", "password": "secret1"})
    assert "10.50.0.40" in allowed()  # short window to pay
    db.run("UPDATE temp_grants SET expires_at=0 WHERE mac='cc:cc:cc:cc:cc:01'")
    access.sync()
    assert "10.50.0.40" not in allowed()
    sid = list(FakeStripe.sessions)[-1]
    r = c.get(f"/paid?session_id={sid}")
    assert "Waiting for payment" in r.text
    # checkout grace is rate-limited per device
    for _ in range(5):
        r = c.post("/buy", data={"plan_id": day})
    assert "Too many attempts" in r.text


def test_11_expiry_removes_access():
    uid = db.one("SELECT id FROM users WHERE email='guest@example.com'")["id"]
    db.run("UPDATE passes SET ends_at=? WHERE user_id=?", (int(time.time()) - 1, uid))
    access.sync()
    assert "10.50.0.21" not in allowed() and "10.50.0.77" not in allowed()
    assert not access.has_access(uid)


def test_12_admin_pages_and_comp_pass():
    c = owner()
    r = c.get("/login")
    c.post("/login", data={"password": "hunter2hunter2", "csrf": csrf(r)})
    for path in ("/", "/guests", "/plans", "/plans/1", "/plans/0", "/settings"):
        assert c.get(path).status_code == 200, path
    uid = db.one("SELECT id FROM users WHERE email='guest@example.com'")["id"]
    page = c.get(f"/guests/{uid}")
    assert page.status_code == 200
    r = c.post(f"/guests/{uid}/action", data={"csrf": csrf(page), "action": "grant", "plan_id": 1, "hours": 5})
    assert r.status_code == 303
    assert access.has_access(uid)
    # edit a plan price and device limit
    page = c.get("/plans/1")
    c.post("/plans/1", data={"csrf": csrf(page), "name": "Day Pass", "price": "12.50", "max_devices": "4",
                             "kind": "one_time", "duration_mode": "hours", "duration_value": "1",
                             "duration_unit": "days", "active": "on", "sort": "1"})
    p = db.one("SELECT * FROM plans WHERE id=1")
    assert p["price_cents"] == 1250 and p["max_devices"] == 4 and p["duration_hours"] == 24
    # settings keep secrets when left blank
    page = c.get("/settings")
    c.post("/settings", data={"csrf": csrf(page), "site_name": "Renamed Lake", "stripe_secret_key": ""})
    assert db.get("site_name") == "Renamed Lake" and db.get("stripe_secret_key") == "sk_test_x"
    # bad csrf is ignored
    c.post("/settings", data={"csrf": "nope", "site_name": "Hacked"})
    assert db.get("site_name") == "Renamed Lake"


def test_13_captive_api():
    c = guest("10.50.0.88", "dd:dd:dd:dd:dd:01")
    r = c.get("/api/captive")
    assert r.json()["captive"] is True


# ---- device rules, bypass, limits, tiers --------------------------------------

def _buy(c, plan_name, email=None, password="secret1"):
    pid = db.one("SELECT id FROM plans WHERE name=?", (plan_name,))["id"]
    if email:
        c.post("/start", data={"plan_id": pid, "email": email, "password": password})
    else:
        c.post("/buy", data={"plan_id": pid})
    sid = list(FakeStripe.sessions)[-1]
    FakeStripe.pay(sid)
    return c.get(f"/paid?session_id={sid}")


def _uid(email):
    return db.one("SELECT id FROM users WHERE email=?", (email,))["id"]


def _admin():
    c = owner()
    r = c.get("/login")
    c.post("/login", data={"password": "hunter2hunter2", "csrf": csrf(r)})
    return c


def test_14_paying_from_new_device_when_full_offers_swap():
    db.set("swap_cooldown_minutes", "60")
    a = guest("10.50.1.1", "10:00:00:00:00:01")
    r = _buy(a, "Weekend Pass", email="fam@example.com")      # 2-device plan
    assert r.headers["location"] == "/account?msg=paid"
    b = guest("10.50.1.2", "10:00:00:00:00:02")
    b.post("/login", data={"email": "fam@example.com", "password": "secret1"})
    uid = _uid("fam@example.com")
    db.run("UPDATE users SET last_swap_at=? WHERE id=?", (int(time.time()), uid))  # mid-cooldown
    c = guest("10.50.1.3", "10:00:00:00:00:03")
    c.post("/login", data={"email": "fam@example.com", "password": "secret1"})
    r = _buy(c, "Weekend Pass")   # (Day Pass allows 4 devices after test_12 edited it)
    assert r.headers["location"] == "/account?msg=paidlimit"
    page = c.get("/account?msg=paidlimit")
    assert "Payment received" in page.text and "Swap for this device" in page.text
    dev_a = db.one("SELECT id FROM devices WHERE mac='10:00:00:00:00:01'")["id"]
    r = c.post("/device/add", data={"replace_id": dev_a})          # allowed despite cooldown
    assert r.headers["location"] == "/account?msg=online"
    assert "10.50.1.3" in allowed() and "10.50.1.1" not in allowed()


def test_15_swap_cooldown():
    uid = _uid("fam@example.com")
    c = guest("10.50.1.3", "10:00:00:00:00:03")
    c.post("/login", data={"email": "fam@example.com", "password": "secret1"})
    dev_b = db.one("SELECT id FROM devices WHERE mac='10:00:00:00:00:02'")["id"]
    c.post("/device/remove", data={"device_id": dev_b})
    assert access.cooldown_left(uid) > 0
    a = guest("10.50.1.1", "10:00:00:00:00:01")
    r = a.post("/login", data={"email": "fam@example.com", "password": "secret1"})
    assert r.headers["location"] == "/account?msg=cooldown"
    assert "min" in a.get("/account?msg=cooldown").text
    # owner lifts it
    adm = _admin()
    page = adm.get(f"/guests/{uid}")
    adm.post(f"/guests/{uid}/action", data={"csrf": csrf(page), "action": "clear_cooldown"})
    r = a.post("/device/add", data={})
    assert r.headers["location"] == "/account?msg=online"
    # cooldown 0 = off
    db.set("swap_cooldown_minutes", "0")
    assert access.cooldown_left(uid) == 0


def test_16_manual_mac_and_other_accounts():
    from app.gateway import valid_mac
    assert valid_mac("3C-22-FB-1A-2B-3C") == "3c:22:fb:1a:2b:3c"
    assert valid_mac("3c22.fb1a.2b3c") == "3c:22:fb:1a:2b:3c"
    assert valid_mac("01:00:5e:00:00:01") is None and valid_mac("nope") is None
    owner_c = guest("10.50.2.1", "20:00:00:00:00:01")
    _buy(owner_c, "Weekend Pass", email="tv@example.com")
    r = owner_c.post("/device/manual", data={"mac": "bad", "label": "TV"})
    assert r.headers["location"] == "/account?msg=badmac"
    r = owner_c.post("/device/manual", data={"mac": "20-00-00-00-00-99", "label": "Cabin TV"})
    assert r.headers["location"] == "/account?msg=manualok"
    d = db.one("SELECT * FROM devices WHERE mac='20:00:00:00:00:99'")
    assert d["source"] == "manual" and d["user_id"] == _uid("tv@example.com")
    DryRun.fake_arp["10.50.2.99"] = "20:00:00:00:00:99"     # the TV comes online
    access.sync()
    assert "10.50.2.99" in allowed()
    # someone else can't steal it by typing its MAC...
    thief = guest("10.50.2.50", "20:00:00:00:00:50")
    _buy(thief, "Day Pass", email="thief@example.com")
    r = thief.post("/device/manual", data={"mac": "20:00:00:00:00:99"})
    assert r.headers["location"] == "/account?msg=taken"
    assert db.one("SELECT user_id FROM devices WHERE mac='20:00:00:00:00:99'")["user_id"] == _uid("tv@example.com")
    # ...but logging in on the device itself moves it (e.g. a kid's phone)
    kid = guest("10.50.2.1", "20:00:00:00:00:01")
    kid.cookies.clear()
    r = kid.post("/login", data={"email": "thief@example.com", "password": "secret1"})
    assert r.headers["location"] == "/account?msg=moved"
    assert db.one("SELECT user_id FROM devices WHERE mac='20:00:00:00:00:01'")["user_id"] == _uid("thief@example.com")
    # guest MAC entry can be switched off
    db.set("guest_manual_mac", "0")
    r = owner_c.post("/device/manual", data={"mac": "20:00:00:00:00:98"})
    assert db.one("SELECT id FROM devices WHERE mac='20:00:00:00:00:98'") is None
    db.set("guest_manual_mac", "1")


def test_17_admin_device_limit_override_and_add_mac():
    uid = _uid("tv@example.com")
    assert access.device_limit(uid) == 2
    adm = _admin()
    page = adm.get(f"/guests/{uid}")
    adm.post(f"/guests/{uid}/action", data={"csrf": csrf(page), "action": "set_limit", "limit": "4"})
    assert access.device_limit(uid) == 4
    adm.post(f"/guests/{uid}/action", data={"csrf": csrf(page), "action": "add_mac",
                                            "mac": "20:00:00:00:00:77", "label": "Console"})
    assert db.one("SELECT user_id FROM devices WHERE mac='20:00:00:00:00:77'")["user_id"] == uid
    assert "custom limit" in adm.get(f"/guests/{uid}").text
    adm.post(f"/guests/{uid}/action", data={"csrf": csrf(page), "action": "set_limit", "limit": ""})
    assert access.device_limit(uid) == 2
    # with the limit back at 2, only the 2 newest devices are let online
    DryRun.fake_arp["10.50.2.77"] = "20:00:00:00:00:77"
    access.sync()
    assert "10.50.2.77" in allowed()


def test_18_bypass_list():
    DryRun.fake_arp["10.50.3.5"] = "30:00:00:00:00:05"
    access.sync()
    assert "10.50.3.5" not in allowed()
    adm = _admin()
    page = adm.get("/bypass")
    assert page.status_code == 200
    adm.post("/bypass", data={"csrf": csrf(page), "mac": "30-00-00-00-00-05", "label": "Office PC"})
    assert "10.50.3.5" in allowed()
    assert "Office PC" in adm.get("/bypass").text
    bid = db.one("SELECT id FROM bypass WHERE mac='30:00:00:00:00:05'")["id"]
    adm.post("/bypass", data={"csrf": csrf(page), "action": "remove", "bypass_id": bid})
    assert "10.50.3.5" not in allowed()


def test_19_speed_tiers():
    db.set("speed_tiers", "Basic = paid_basic\nPremium = paid_premium")
    adm = _admin()
    monthly = db.one("SELECT * FROM plans WHERE name='Monthly'")
    page = adm.get(f"/plans/{monthly['id']}")
    assert "paid_premium" in page.text
    adm.post(f"/plans/{monthly['id']}", data={
        "csrf": csrf(page), "name": "Monthly", "price": "60", "max_devices": "3", "kind": "subscription",
        "interval": "month", "interval_count": "1", "stop_at_season_end": "on", "active": "on", "sort": "3",
        "speed_tier": "Premium", "duration_mode": "hours", "duration_value": "30", "duration_unit": "days"})
    assert db.one("SELECT speed_tier FROM plans WHERE id=?", (monthly["id"],))["speed_tier"] == "Premium"
    c = guest("10.50.4.1", "40:00:00:00:00:01")
    _buy(c, "Monthly", email="fast@example.com")
    assert "10.50.4.1" in allowed() and "10.50.4.1" in allowed("paid_premium")
    assert "10.50.4.1" not in allowed("paid_basic")
    # untiered plans only land in the base alias
    assert "10.50.2.99" in allowed() and "10.50.2.99" not in allowed("paid_premium")
    # access ends -> removed from every alias
    db.run("UPDATE passes SET ends_at=? WHERE user_id=?", (int(time.time()) - 1, _uid("fast@example.com")))
    access.sync()
    assert "10.50.4.1" not in allowed() and "10.50.4.1" not in allowed("paid_premium")
    assert "Premium" in adm.get("/plans").text


def test_20_migrations_present():
    for table, col, _ in db.MIGRATIONS:
        cols = {r["name"] for r in db.q(f"PRAGMA table_info({table})")}
        assert col in cols


# ---- extra device slots and join codes -----------------------------------------

def _set_extras(name, price, max_x=3, mode="none", val=0, floor=0):
    db.run("UPDATE plans SET extra_device_cents=?, max_extra_devices=?, extra_discount_mode=?, "
           "extra_discount_value=?, extra_min_cents=? WHERE name=?", (price, max_x, mode, val, floor, name))
    return db.one("SELECT * FROM plans WHERE name=?", (name,))


def test_21_slot_pricing():
    from app import extras as xs
    p = _set_extras("Weekend Pass", 300, 4)
    assert xs.price_list(p) == [300, 300, 300, 300]
    p = _set_extras("Weekend Pass", 300, 4, "percent", 10)
    assert xs.price_list(p) == [300, 270, 240, 210]
    p = _set_extras("Weekend Pass", 300, 4, "amount", 50, floor=200)
    assert xs.price_list(p) == [300, 250, 200, 200]
    assert xs.full_price(p, 1, 2) == 450                    # 2nd + 3rd slot
    assert "extra devices" in xs.describe(p)
    p = _set_extras("Weekend Pass", 0)
    assert not xs.enabled(p) and xs.describe(p) == ""


def test_22_extras_at_checkout_one_time():
    _set_extras("Weekend Pass", 300, 3, "percent", 10)
    c = guest("10.50.5.1", "50:00:00:00:00:01")
    wk = db.one("SELECT id FROM plans WHERE name='Weekend Pass'")["id"]
    page = c.get("/")
    assert "No extra devices" in page.text and "+2 devices (+$5.70)" in page.text
    c.post("/start", data={"plan_id": wk, "email": "big@example.com", "password": "secret1", "extra": 2})
    sid = list(FakeStripe.sessions)[-1]
    items = FakeStripe.sessions[sid]["params"]["line_items"]
    assert [i["price_data"]["unit_amount"] for i in items] == [2500, 570]
    FakeStripe.pay(sid)
    c.get(f"/paid?session_id={sid}")
    uid = _uid("big@example.com")
    assert access.device_limit(uid) == 4                   # 2 included + 2 bought
    assert db.one("SELECT amount_cents FROM passes WHERE stripe_session_id=?", (sid,))["amount_cents"] == 3070
    # can't buy more extras than the plan allows
    c.post("/buy", data={"plan_id": wk, "extra": 9})
    sid = list(FakeStripe.sessions)[-1]
    assert FakeStripe.sessions[sid]["params"]["line_items"][1]["price_data"]["unit_amount"] == 810  # capped at 3


def test_23_extra_slots_later_are_prorated():
    from app import extras as xs
    uid = _uid("big@example.com")
    # half of the 72 h weekend left
    now = int(time.time())
    db.run("UPDATE passes SET ends_at=? WHERE user_id=? AND status='active'", (now + 36 * 3600, uid))
    q = xs.quote_later(uid)
    assert q["held"] == 2 and q["max_more"] == 1
    n, cents, full = q["options"][0]
    assert full == 240 and abs(cents - 120) <= 1          # 3rd slot $2.40, half the time left
    c = guest("10.50.5.1", "50:00:00:00:00:01")
    c.post("/login", data={"email": "big@example.com", "password": "secret1"})
    assert "Buy device slots" in c.get("/account").text
    c.post("/slots", data={"n": 1})
    sid = list(FakeStripe.sessions)[-1]
    params = FakeStripe.sessions[sid]["params"]
    assert params["mode"] == "payment" and abs(params["line_items"][0]["price_data"]["unit_amount"] - 120) <= 1
    FakeStripe.pay(sid)
    r = c.get(f"/paid?session_id={sid}")
    assert r.headers["location"] == "/account?msg=slots"
    assert access.device_limit(uid) == 5
    addon = db.one("SELECT * FROM passes WHERE stripe_session_id=?", (sid,))
    assert addon["kind"] == "addon" and addon["ends_at"] == access.access_until(uid)
    # all extras used: no more offered; asking anyway is refused
    assert xs.quote_later(uid)["options"] == []
    assert c.post("/slots", data={"n": 1}).headers["location"] == "/account?msg=noslots"
    # adding time doesn't break anything; extras end with the pass they were bought for
    db.run("UPDATE passes SET ends_at=? WHERE user_id=?", (now - 1, uid))
    access.sync()
    assert access.device_limit(uid) == 0


def test_24_extras_on_subscriptions():
    from app import extras as xs
    _set_extras("Monthly", 500, 3, "amount", 100, floor=300)   # $5, $4, $3
    c = guest("10.50.6.1", "60:00:00:00:00:01")
    mid = db.one("SELECT id FROM plans WHERE name='Monthly'")["id"]
    c.post("/start", data={"plan_id": mid, "email": "subx@example.com", "password": "secret1", "extra": 1})
    sid = list(FakeStripe.sessions)[-1]
    items = FakeStripe.sessions[sid]["params"]["line_items"]
    assert items[1]["price_data"]["recurring"]["interval"] == "month"
    assert items[1]["price_data"]["product_data"]["metadata"] == {"passgate": "extras", "count": "1"}
    FakeStripe.pay(sid)
    c.get(f"/paid?session_id={sid}")
    uid = _uid("subx@example.com")
    assert access.device_limit(uid) == 4                       # 3 included + 1
    q = xs.quote_later(uid)
    assert q["recurring"] and [o[2] for o in q["options"]] == [400, 700]
    c.post("/slots", data={"n": 2})
    sid2 = list(FakeStripe.sessions)[-1]
    assert FakeStripe.sessions[sid2]["params"]["mode"] == "payment"   # prorated part via Checkout
    FakeStripe.pay(sid2)
    c.get(f"/paid?session_id={sid2}")
    sub = FakeStripe.subs[f"sub_{sid}"]
    added = sub["items"]["data"][-1]
    assert added["kw"]["proration_behavior"] == "none" and added["price"]["unit_amount"] == 700
    assert access.device_limit(uid) == 6
    billing.reconcile()                                        # Stripe stays the source of truth
    assert access.device_limit(uid) == 6
    sub["items"]["data"].pop()                                 # owner removes the item in Stripe
    billing.reconcile()
    assert access.device_limit(uid) == 4


def test_25_override_plus_extras():
    uid = _uid("subx@example.com")
    db.run("UPDATE users SET device_limit_override=5 WHERE id=?", (uid,))
    assert access.device_limit(uid) == 6                       # your 5 + 1 bought
    db.run("UPDATE users SET device_limit_override=NULL WHERE id=?", (uid,))


def test_26_join_codes():
    parent = guest("10.50.7.1", "70:00:00:00:00:01")
    _buy(parent, "Weekend Pass", email="parent@example.com")
    uid = _uid("parent@example.com")
    r = parent.post("/invite", data={"label": "Emma's iPad"})
    assert r.headers["location"] == "/account?msg=invite"
    page = parent.get("/account?msg=invite")
    code = re.search(r"letter-spacing:\.15em\">([A-Z0-9]{3}-[A-Z0-9]{3})<", page.text).group(1)
    kid = guest("10.50.7.2", "70:00:00:00:00:02")
    assert "Check it and try again" in kid.post("/join", data={"code": "ZZZ-ZZZ"}).text
    r = kid.post("/join", data={"code": code.lower().replace("-", " ")})   # forgiving input
    assert "You're in" in r.text and "pa•••@example.com" in r.text
    d = db.one("SELECT * FROM devices WHERE mac='70:00:00:00:00:02'")
    assert d["user_id"] == uid and d["source"] == "invite" and d["label"] == "Emma's iPad"
    assert "10.50.7.2" in allowed()
    # the kid's device never gets the parent's account
    assert kid.get("/account").headers["location"] == "/"
    home = kid.get("/")
    assert home.status_code == 200 and "Connected through" in home.text and "Log out" not in home.text
    # codes are single use
    other = guest("10.50.7.3", "70:00:00:00:00:03")
    assert "Check it and try again" in other.post("/join", data={"code": code}).text
    # full account -> clear message
    parent.post("/invite", data={})
    code2 = re.search(r"letter-spacing:\.15em\">([A-Z0-9]{3}-[A-Z0-9]{3})<",
                      parent.get("/account?msg=invite").text).group(1)
    db.run("UPDATE plans SET extra_device_cents=0 WHERE name='Weekend Pass'")
    third = guest("10.50.7.4", "70:00:00:00:00:04")
    assert access.device_limit(uid) == 2
    assert "no free device slots" in third.post("/join", data={"code": code2}).text
    # brute force is slowed down
    pest = guest("10.50.7.9", "70:00:00:00:00:09")
    for _ in range(12):
        r = pest.post("/join", data={"code": "AAA-AAA"})
    assert "Too many wrong codes" in r.text


def test_27_admin_plan_extras_form():
    adm = _admin()
    wk = db.one("SELECT * FROM plans WHERE name='Day Pass'")
    page = adm.get(f"/plans/{wk['id']}")
    assert "Extra device slots" in page.text
    adm.post(f"/plans/{wk['id']}", data={
        "csrf": csrf(page), "name": "Day Pass", "price": "12.50", "max_devices": "2", "kind": "one_time",
        "duration_mode": "hours", "duration_value": "1", "duration_unit": "days", "active": "on", "sort": "1",
        "extras_on": "on", "extra_price": "2.00", "max_extra_devices": "5", "extra_discount_mode": "percent",
        "extra_discount_pct": "25", "extra_discount_amt": "0", "extra_min": "1.00"})
    p = db.one("SELECT * FROM plans WHERE id=?", (wk["id"],))
    assert (p["extra_device_cents"], p["max_extra_devices"], p["extra_discount_mode"], p["extra_discount_value"],
            p["extra_min_cents"]) == (200, 5, "percent", 25, 100)
    from app import extras as xs
    assert xs.price_list(p) == [200, 150, 100, 100, 100]
    assert "$1.50" in adm.get("/plans").text
    # unticking turns extras off
    page = adm.get(f"/plans/{wk['id']}")
    adm.post(f"/plans/{wk['id']}", data={
        "csrf": csrf(page), "name": "Day Pass", "price": "12.50", "max_devices": "2", "kind": "one_time",
        "duration_mode": "hours", "duration_value": "1", "duration_unit": "days", "active": "on", "sort": "1"})
    assert db.one("SELECT extra_device_cents FROM plans WHERE id=?", (wk["id"],))["extra_device_cents"] == 0


# ---- free-access promotion -------------------------------------------------------

def _local(offset_s):
    from datetime import datetime
    from app.util import tz
    return datetime.fromtimestamp(time.time() + offset_s, tz()).strftime("%Y-%m-%dT%H:%M")


def test_28_free_promotion():
    adm = _admin()
    page = adm.get("/promo")
    assert page.status_code == 200 and "Free-access promotion" in page.text
    c = guest("10.50.8.1", "80:00:00:00:00:01")
    # off: no free button, and claiming is refused
    assert "Connect free" not in c.get("/").text
    assert "ended" in c.post("/free").headers["location"]
    adm.post("/promo", data={"csrf": csrf(page), "promo_enabled": "on", "promo_start": _local(-3600),
                             "promo_end": _local(2 * 3600), "promo_title": "Long weekend on us!",
                             "promo_text": "Enjoy. Like it? Grab the monthly.", "promo_after_text": "Hope you enjoyed it!",
                             "promo_after_days": "14"})
    home = c.get("/")
    assert "Long weekend on us!" in home.text and "Connect free until" in home.text and "Monthly" in home.text
    assert "Running now" in adm.get("/promo").text
    r = c.post("/free")
    assert r.headers["location"] == "/?msg=free"
    assert "10.50.8.1" in allowed()
    assert "You're connected" in c.get("/").text.replace("&#39;", "'")
    assert "promotion is running" in adm.get("/").text
    # the guest can still buy a plan during the promotion
    assert "Continue to payment" in c.get("/").text
    # end it early: device loses access, then sees the thank-you note and plans
    page = adm.get("/promo")
    adm.post("/promo", data={"csrf": csrf(page), "action": "end"})
    assert "10.50.8.1" not in allowed()
    home = c.get("/")
    assert "Hope you enjoyed it!" in home.text and "Connect free" not in home.text
    # another device that never used it doesn't see the note
    assert "Hope you enjoyed it!" not in guest("10.50.8.2", "80:00:00:00:00:02").get("/").text
    # new dates start a fresh list
    page = adm.get("/promo")
    adm.post("/promo", data={"csrf": csrf(page), "promo_enabled": "on", "promo_start": _local(86400),
                             "promo_end": _local(3 * 86400)})
    assert db.one("SELECT COUNT(*) n FROM promo_devices")["n"] == 0
    assert "Scheduled" in adm.get("/promo").text
    # bad dates are flagged
    page = adm.get("/promo")
    r = adm.post("/promo", data={"csrf": csrf(page), "promo_enabled": "on", "promo_start": _local(7200),
                                 "promo_end": _local(3600)})
    assert "won%27t+run" in r.headers["location"] or "won't" in r.headers["location"]
