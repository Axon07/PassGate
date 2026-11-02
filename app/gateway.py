"""Gateway drivers: the part that actually opens and closes the gate.

PassGate keeps the list of devices that should be online. A driver pushes
that list to the router. Drivers only need four operations, so adding one
for another router (pfSense, MikroTik...) is a small job.
"""
import logging
import re

import httpx

from . import db

log = logging.getLogger("passgate.gateway")
MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


def norm_mac(mac: str) -> str:
    return (mac or "").strip().lower().replace("-", ":")


def valid_mac(mac: str) -> str | None:
    """Normalise a typed MAC (aa:bb:.., AA-BB-.., aabb.ccdd.eeff, aabbccddeeff).
    Returns None for junk, all-zero, broadcast and multicast addresses."""
    m = re.sub(r"[^0-9a-f]", "", (mac or "").lower())
    if len(m) != 12 or m in ("000000000000", "ffffffffffff"):
        return None
    if int(m[:2], 16) & 1:  # multicast bit set: not a real device
        return None
    return ":".join(m[i:i + 2] for i in range(0, 12, 2))


def base_alias() -> str:
    return db.get("opnsense_alias") or "paid_clients"


def speed_tiers() -> list[tuple[str, str]]:
    """[(tier name, alias)] from the 'speed_tiers' setting, in priority order
    (later lines win when a guest has passes in more than one tier)."""
    out = []
    for line in (db.get("speed_tiers") or "").splitlines():
        if "=" in line:
            name, alias = (x.strip() for x in line.split("=", 1))
            if name and re.fullmatch(r"[A-Za-z0-9_]{1,31}", alias or ""):
                out.append((name, alias))
    return out


class Driver:
    """Every method takes the alias (firewall table) to act on. The base
    alias decides who gets internet; tier aliases only decide speed."""
    name = "base"

    def list_allowed(self, alias: str) -> set[str]:
        raise NotImplementedError

    def allow(self, alias: str, ip: str):
        raise NotImplementedError

    def revoke(self, alias: str, ip: str, kill_states: bool = True):
        raise NotImplementedError

    def arp_table(self) -> dict[str, str]:
        """Return {ip: mac} for guests the router can currently see."""
        return {}

    def test(self) -> str:
        return "ok"


class DryRun(Driver):
    """Doesn't touch any router. For trying the portal out and for tests."""
    name = "dryrun"
    tables: dict[str, set[str]] = {}
    fake_arp: dict[str, str] = {}

    def list_allowed(self, alias):
        return set(DryRun.tables.get(alias, set()))

    def allow(self, alias, ip):
        DryRun.tables.setdefault(alias, set()).add(ip)

    def revoke(self, alias, ip, kill_states=True):
        DryRun.tables.setdefault(alias, set()).discard(ip)

    def arp_table(self):
        return dict(DryRun.fake_arp)

    def test(self):
        return "Dry-run mode: no router is being controlled."


class OPNsense(Driver):
    """Controls OPNsense 'External (advanced)' aliases via the API.

    Guests whose IP is in the base alias get internet; everyone else is
    redirected to the portal by a NAT rule. Optional tier aliases are used
    by traffic-shaper rules. See docs/opnsense-setup.md.
    """
    name = "opnsense"

    def __init__(self):
        self.base = db.get("opnsense_url").rstrip("/")
        self.client = httpx.Client(
            auth=(db.get("opnsense_key"), db.get("opnsense_secret")),
            verify=db.get("opnsense_verify_tls") == "1",
            timeout=10,
        )

    def _get(self, path):
        r = self.client.get(f"{self.base}/api/{path}")
        r.raise_for_status()
        return r.json()

    def _post(self, path, data=None):
        r = self.client.post(f"{self.base}/api/{path}", json=data or {})
        r.raise_for_status()
        return r.json() if r.content else {}

    def list_allowed(self, alias):
        data = self._get(f"firewall/alias_util/list/{alias}")
        rows = data.get("rows", []) if isinstance(data, dict) else []
        return {r.get("ip") for r in rows if r.get("ip")}

    def allow(self, alias, ip):
        self._post(f"firewall/alias_util/add/{alias}", {"address": ip})

    def revoke(self, alias, ip, kill_states=True):
        self._post(f"firewall/alias_util/delete/{alias}", {"address": ip})
        if not kill_states:
            return
        # Drop open connections so access really stops (best effort; the
        # endpoint name differs between OPNsense releases).
        for path in ("diagnostics/firewall/kill_states", "diagnostics/firewall/killStates"):
            try:
                self._post(path, {"filter": ip})
                break
            except Exception:
                continue

    def arp_table(self):
        rows = None
        for method, path in (("get", "diagnostics/interface/get_arp"),
                             ("get", "diagnostics/interface/getArp"),
                             ("post", "diagnostics/interface/search_arp")):
            try:
                data = self._get(path) if method == "get" else self._post(path, {"rowCount": -1})
                rows = data.get("rows", data) if isinstance(data, dict) else data
                if isinstance(rows, list):
                    break
            except Exception:
                continue
        out = {}
        for r in rows or []:
            ip, mac = r.get("ip"), norm_mac(r.get("mac", ""))
            if ip and MAC_RE.match(mac) and not r.get("expired"):
                out[ip] = mac
        return out

    def test(self):
        parts = []
        for alias in [base_alias()] + [a for _, a in speed_tiers()]:
            try:
                parts.append(f"'{alias}': {len(self.list_allowed(alias))} entries")
            except Exception as e:
                parts.append(f"'{alias}': FAILED ({e})")
        arp = self.arp_table()
        return "Connected. Aliases " + ", ".join(parts) + f"; router sees {len(arp)} devices in its ARP table."


class OpenWrt(Driver):
    """Controls an nftables set on an OpenWrt router (22.03 or newer, fw4).

    PassGate talks to the router's web server (uhttpd's /ubus endpoint, the
    same one LuCI uses) as a dedicated login that may run exactly one
    command: /usr/libexec/passgate-gate, a small helper installed by
    openwrt/setup.sh that adds and removes IPs in the set and prints the ARP
    table. Firewall rules let IPs in the set out to the internet and send
    everyone else's web traffic to the portal. See docs/openwrt-setup.md.
    """
    name = "openwrt"
    HELPER = "/usr/libexec/passgate-gate"
    NO_SESSION = "0" * 32
    transport = None           # tests plug in a fake router here
    _sessions: dict = {}       # (url, user, password) -> ubus session id
    HINTS = {
        3: "the router doesn't know that call (is rpcd running?)",
        4: "not found: run openwrt/setup.sh on the router (helper or rpcd file plugin missing)",
        6: "permission denied: check the user name and password, or run openwrt/setup.sh again",
        7: "the router took too long to answer",
    }

    def __init__(self):
        self.url = (db.get("openwrt_url") or "").rstrip("/") + "/ubus"
        self.user = db.get("openwrt_user") or "passgate"
        self.password = db.get("openwrt_password") or ""
        kw = {"transport": self.transport} if self.transport else {}
        self.client = httpx.Client(verify=db.get("openwrt_verify_tls") == "1", timeout=10, **kw)
        self.key = (self.url, self.user, self.password)

    def _call(self, sid, obj, method, args):
        r = self.client.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": "call",
                                             "params": [sid, obj, method, args]})
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            return -1, data["error"].get("message", "error")
        res = data.get("result") or [-1]
        return res[0], (res[1] if len(res) > 1 else {})

    def _login(self):
        status, out = self._call(self.NO_SESSION, "session", "login",
                                 {"username": self.user, "password": self.password, "timeout": 3600})
        if status != 0:
            raise RuntimeError("OpenWrt login failed: " + (
                "wrong user name or password, or setup.sh hasn't been run" if status in (6, -1)
                else self.HINTS.get(status, f"ubus status {status}")))
        OpenWrt._sessions[self.key] = out["ubus_rpc_session"]
        return out["ubus_rpc_session"]

    def _run(self, *args) -> str:
        for attempt in (0, 1):
            sid = OpenWrt._sessions.get(self.key) if attempt == 0 else None
            sid = sid or self._login()
            status, out = self._call(sid, "file", "exec", {"command": self.HELPER, "params": list(args)})
            if status in (-1, 6) and attempt == 0:
                OpenWrt._sessions.pop(self.key, None)   # session expired (router rebooted?): log in again
                continue
            if status != 0:
                raise RuntimeError("OpenWrt: " + self.HINTS.get(status, f"ubus status {status}") +
                                   (f" ({out})" if isinstance(out, str) else ""))
            if out.get("code", 0) != 0:
                raise RuntimeError(f"passgate-gate {args[0]} failed: {(out.get('stderr') or '').strip()}")
            return out.get("stdout") or ""
        raise RuntimeError("OpenWrt: not allowed")

    def list_allowed(self, alias):
        m = re.search(r"elements\s*=\s*\{([^}]*)\}", self._run("list", alias))
        return set(re.findall(r"\d{1,3}(?:\.\d{1,3}){3}", m.group(1))) if m else set()

    def allow(self, alias, ip):
        self._run("allow", alias, ip)

    def revoke(self, alias, ip, kill_states=True):
        self._run("revoke", alias, ip, *(["kill"] if kill_states else []))

    def arp_table(self):
        return parse_proc_arp(self._run("arp"))

    def test(self):
        version = self._run("version").strip()
        parts = []
        for alias in [base_alias()] + [a for _, a in speed_tiers()]:
            try:
                parts.append(f"'{alias}': {len(self.list_allowed(alias))} entries")
            except Exception as e:
                parts.append(f"'{alias}': FAILED ({e})")
        arp = self.arp_table()
        return (f"Connected ({version}). Sets " + ", ".join(parts) +
                f"; router sees {len(arp)} devices in its ARP table.")


DRIVERS = {"opnsense": OPNsense, "openwrt": OpenWrt}


def get_driver() -> Driver:
    return DRIVERS.get(db.get("gateway_driver"), DryRun)()


def parse_proc_arp(text: str) -> dict[str, str]:
    """{ip: mac} from the text of /proc/net/arp, skipping incomplete entries."""
    out = {}
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4 and parts[2] != "0x0":
            mac = norm_mac(parts[3])
            if MAC_RE.match(mac) and mac != "00:00:00:00:00:00":
                out[parts[0]] = mac
    return out


def local_arp() -> dict[str, str]:
    """If the portal sits on the guest network itself, the container's own
    ARP cache already knows each guest's MAC. Fast and needs no API call."""
    try:
        with open("/proc/net/arp") as f:
            return parse_proc_arp(f.read())
    except Exception:
        return {}


def mac_for_ip(ip: str) -> str | None:
    mac = local_arp().get(ip)
    if mac:
        return mac
    try:
        mac = get_driver().arp_table().get(ip)
    except Exception as e:  # router unreachable
        log.warning("ARP lookup failed: %s", e)
        mac = None
    if mac:
        return mac
    if db.get("gateway_driver") == "dryrun":
        # No router to ask: use the IP as the identity so testing still works.
        return "ip-" + ip
    return None
