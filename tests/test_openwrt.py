"""OpenWrt driver against a fake router that speaks uhttpd's /ubus JSON-RPC
the way rpcd does (login, sessions, an exec allow-list)."""
import json
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="passgate-test-"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from app import access, db, gateway  # noqa: E402
from app.gateway import OpenWrt  # noqa: E402

db.init()

ARP = """IP address       HW type     Flags       HW address            Mask     Device
10.50.0.5        0x1         0x2         aa:bb:cc:dd:ee:01     *        br-guest
10.50.0.6        0x1         0x2         AA-BB-CC-DD-EE-02     *        br-guest
10.50.0.7        0x1         0x0         00:00:00:00:00:00     *        br-guest
"""


class FakeRouter:
    def __init__(self, user="passgate", password="s3cret"):
        self.user, self.password = user, password
        self.sessions = set()
        self.sets = {"paid_clients": set()}
        self.calls = []
        self.logins = 0

    def nft_list(self, name):
        els = ", ".join(sorted(self.sets[name]))
        body = f"\t\telements = {{ {els} }}\n" if els else ""
        return f"table inet fw4 {{\n\tset {name} {{\n\t\ttype ipv4_addr\n{body}\t}}\n}}\n"

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ubus"
        msg = json.loads(request.content)
        sid, obj, method, args = msg["params"]
        self.calls.append((obj, method, args))

        def reply(*result):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": list(result)})

        if (obj, method) == ("session", "login"):
            if args["username"] != self.user or args["password"] != self.password:
                return reply(6)
            self.logins += 1
            new = f"{self.logins:032x}"
            self.sessions.add(new)
            return reply(0, {"ubus_rpc_session": new, "timeout": args.get("timeout", 300)})
        if sid not in self.sessions:   # what rpcd says for an unknown/expired session
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"],
                                             "error": {"code": -32002, "message": "Access denied"}})
        if (obj, method) != ("file", "exec") or args["command"] != OpenWrt.HELPER:
            return reply(6)
        p = args.get("params", [])
        if p[0] == "version":
            return reply(0, {"code": 0, "stdout": "passgate-gate 1\n"})
        if p[0] == "arp":
            return reply(0, {"code": 0, "stdout": ARP})
        name = p[1]
        if name not in self.sets:
            return reply(0, {"code": 1, "stderr": f"Error: No such file or directory\nlist set inet fw4 {name}\n"})
        if p[0] == "list":
            return reply(0, {"code": 0, "stdout": self.nft_list(name)})
        if p[0] == "allow":
            self.sets[name].add(p[2])
        elif p[0] == "revoke":
            self.sets[name].discard(p[2])
        return reply(0, {"code": 0})


@pytest.fixture
def router(monkeypatch):
    r = FakeRouter()
    monkeypatch.setattr(OpenWrt, "transport", httpx.MockTransport(r.handle))
    monkeypatch.setattr(OpenWrt, "_sessions", {})
    old = {k: db.get(k) for k in ("gateway_driver", "openwrt_url", "openwrt_user", "openwrt_password")}
    db.set("gateway_driver", "openwrt")
    db.set("openwrt_url", "http://192.168.1.1/")
    db.set("openwrt_user", "passgate")
    db.set("openwrt_password", "s3cret")
    yield r
    for k, v in old.items():
        db.set(k, v)


def test_driver_basics(router):
    drv = gateway.get_driver()
    assert isinstance(drv, OpenWrt)
    assert drv.list_allowed("paid_clients") == set()
    drv.allow("paid_clients", "10.50.0.5")
    drv.allow("paid_clients", "10.50.0.9")
    assert drv.list_allowed("paid_clients") == {"10.50.0.5", "10.50.0.9"}
    drv.revoke("paid_clients", "10.50.0.5")
    assert router.calls[-1][2]["params"] == ["revoke", "paid_clients", "10.50.0.5", "kill"]
    assert drv.list_allowed("paid_clients") == {"10.50.0.9"}
    # ARP: normalised MACs, incomplete entries skipped
    assert drv.arp_table() == {"10.50.0.5": "aa:bb:cc:dd:ee:01", "10.50.0.6": "aa:bb:cc:dd:ee:02"}
    assert "passgate-gate 1" in drv.test() and "'paid_clients': 1 entries" in drv.test()
    # one login, reused by later driver instances
    gateway.get_driver().list_allowed("paid_clients")
    assert router.logins == 1


def test_relogin_after_router_reboot(router):
    drv = gateway.get_driver()
    drv.allow("paid_clients", "10.50.0.5")
    router.sessions.clear()          # router rebooted: old session id is gone
    assert gateway.get_driver().list_allowed("paid_clients") == {"10.50.0.5"}
    assert router.logins == 2


def test_errors_are_explained(router):
    db.set("openwrt_password", "wrong")
    with pytest.raises(RuntimeError, match="wrong user name or password"):
        gateway.get_driver().test()
    db.set("openwrt_password", "s3cret")
    with pytest.raises(RuntimeError, match="passgate-gate list failed: Error: No such file"):
        gateway.get_driver().list_allowed("no_such_set")


def test_sync_through_openwrt(router):
    """The normal 30-second sync fills the router's set from active passes."""
    now = db.now()
    uid = db.run("INSERT INTO users(email, pw_hash, created_at) VALUES(?,?,?)", ("owrt@example.com", "x", now))
    db.run("INSERT INTO passes(user_id, plan_name, kind, max_devices, status, source, starts_at, ends_at, created_at) "
           "VALUES(?,?,?,?,?,?,?,?,?)", (uid, "Comp", "onetime", 2, "active", "admin", now - 60, now + 3600, now))
    db.run("INSERT INTO devices(user_id, mac, last_ip, added_at) VALUES(?,?,?,?)",
           (uid, "aa:bb:cc:dd:ee:01", "10.50.0.5", now))
    router.sets["paid_clients"].add("10.50.0.99")   # left over, nobody paid for it
    access.sync()
    assert "10.50.0.5" in router.sets["paid_clients"]
    assert "10.50.0.99" not in router.sets["paid_clients"]


def test_setup_script_is_valid_shell():
    import pathlib
    import subprocess
    script = pathlib.Path(__file__).parent.parent / "openwrt" / "setup.sh"
    assert subprocess.run(["sh", "-n", str(script)]).returncode == 0
