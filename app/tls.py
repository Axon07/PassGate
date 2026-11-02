"""Built-in HTTPS for the guest portal.

Two ways to get a certificate:
  * letsencrypt: free certificate from Let's Encrypt using a DNS challenge.
    The portal never has to be reachable from the internet (works behind
    CGNAT); PassGate proves you own the domain by creating a temporary DNS
    record through your DNS provider's API. Cloudflare is one field; any of
    lego's 150+ providers works through the advanced option.
  * files: use your own certificate (fullchain.pem + privkey.pem in
    <data>/tls/), e.g. one exported from another tool.

The certificate is loaded into the running HTTPS server and swapped in
place on renewal, so nothing needs restarting.
"""
import logging
import os
import shutil
import ssl
import subprocess
import threading
from datetime import datetime, timezone

from . import db

log = logging.getLogger("passgate.tls")

TLS_DIR = os.path.join(db.DATA_DIR, "tls")
LEGO_DIR = os.path.join(TLS_DIR, "lego")
OWN_CERT = os.path.join(TLS_DIR, "fullchain.pem")
OWN_KEY = os.path.join(TLS_DIR, "privkey.pem")
LE_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
_lock = threading.Lock()


def mode() -> str:
    m = db.get("tls_mode")
    return m if m in ("letsencrypt", "files") else "off"


def domain() -> str:
    return (db.get("tls_domain") or "").strip().lower().rstrip(".")


def cert_paths() -> tuple[str, str] | None:
    """(certfile, keyfile) if a usable certificate exists for the current mode."""
    if mode() == "files":
        paths = (OWN_CERT, OWN_KEY)
    elif mode() == "letsencrypt" and domain():
        base = os.path.join(LEGO_DIR, "certificates", domain().replace("*", "_"))
        paths = (base + ".crt", base + ".key")
    else:
        return None
    return paths if all(os.path.exists(p) for p in paths) else None


def active() -> bool:
    return cert_paths() is not None


def cert_info() -> dict | None:
    """Subject names and expiry of the certificate in use."""
    paths = cert_paths()
    if not paths:
        return None
    try:
        d = ssl._ssl._test_decode_cert(paths[0])  # stdlib, no extra dependency
        expires = datetime.strptime(d["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        names = [v for k, v in d.get("subjectAltName", ()) if k == "DNS"]
        return {"expires": int(expires.timestamp()), "names": names,
                "days_left": (expires - datetime.now(timezone.utc)).days}
    except Exception as e:
        return {"error": str(e)}


def portal_host() -> str:
    """The hostname guests should use, when HTTPS is on."""
    if mode() == "files":
        info = cert_info() or {}
        return domain() or (info.get("names") or [""])[0]
    return domain()


def portal_url() -> str:
    """Where guests should be sent: https://<domain> once a certificate is
    in place, otherwise the plain portal address from Settings."""
    configured = (db.get("portal_url") or "").rstrip("/")
    if active() and portal_host():
        if configured.startswith("https://"):
            return configured  # e.g. a custom port when testing
        return "https://" + portal_host()
    return configured


# ---- Let's Encrypt via lego ---------------------------------------------------

def lego_binary() -> str | None:
    return shutil.which("lego")


def provider_env() -> tuple[str, dict]:
    """(lego DNS provider name, environment variables for it)."""
    provider = (db.get("tls_dns_provider") or "cloudflare").strip()
    env = {}
    if provider == "cloudflare" and db.get("tls_cloudflare_token"):
        env["CF_DNS_API_TOKEN"] = db.get("tls_cloudflare_token")
    for line in (db.get("tls_dns_env") or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if k.strip():
                env[k.strip()] = v.strip()
    return provider, env


def lego_command(action: str) -> list[str]:
    provider, _ = provider_env()
    cmd = [lego_binary() or "lego", "--accept-tos", "--email", db.get("tls_email").strip(),
           "--dns", provider, "--domains", domain(), "--path", LEGO_DIR,
           "--dns.resolvers", "1.1.1.1:53", "--dns.resolvers", "8.8.8.8:53"]
    if db.get("tls_staging") == "1":
        cmd += ["--server", LE_STAGING]
    if action == "renew":
        cmd += ["renew", "--days", "30", "--no-random-sleep"]
    else:
        cmd += ["run"]
    return cmd


def problems() -> list[str]:
    """What's missing before a certificate can be requested."""
    out = []
    if mode() != "letsencrypt":
        return out
    if not lego_binary():
        out.append("The lego tool isn't installed here. It's included in the PassGate Docker image.")
    if not domain() or "." not in domain():
        out.append("Enter the portal's domain name, e.g. wifi.example.com.")
    if "@" not in (db.get("tls_email") or ""):
        out.append("Enter an email address (Let's Encrypt requires one; it's only sent to them).")
    provider, env = provider_env()
    if provider == "cloudflare" and "CF_DNS_API_TOKEN" not in env:
        out.append("Enter a Cloudflare API token with Zone → DNS → Edit permission.")
    return out


def obtain_or_renew(force: bool = False) -> tuple[bool, str]:
    """Get a certificate if there isn't one, otherwise renew if due.
    Runs lego (can take a minute while DNS propagates)."""
    if mode() != "letsencrypt":
        return False, "Let's Encrypt isn't selected."
    if (p := problems()):
        return False, " ".join(p)
    with _lock:
        have = cert_paths() is not None
        action = "renew" if have and not force else "run"
        provider, extra = provider_env()
        env = {**os.environ, **extra}
        os.makedirs(LEGO_DIR, exist_ok=True)
        try:
            res = subprocess.run(lego_command(action), env=env, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            ok, msg = False, "Timed out waiting for Let's Encrypt (10 minutes)."
        else:
            out = (res.stdout + res.stderr).strip()
            ok = res.returncode == 0
            last = out.splitlines()[-1] if out else ""
            if ok:
                msg = "Certificate is up to date." if action == "renew" and "no renewal" in out.lower() \
                    else "Certificate obtained." if action == "run" else "Certificate renewed."
            else:
                for secret in extra.values():
                    last = last.replace(secret, "•••")
                msg = "Let's Encrypt failed: " + (last[-400:] or f"exit code {res.returncode}")
        db.set("tls_last_result", msg)
        db.set("tls_last_check", db.now())
        db.log("tls", msg)
        (log.info if ok else log.warning)(msg)
        return ok, msg


def due_for_check() -> bool:
    try:
        last = int(db.get("tls_last_check") or 0)
    except ValueError:
        last = 0
    if mode() != "letsencrypt":
        return False
    if not cert_paths():
        return db.now() - last > 3600          # retry hourly until we have one
    return db.now() - last > 12 * 3600         # then check twice a day


# ---- serving ---------------------------------------------------------------------

def ssl_context() -> ssl.SSLContext | None:
    paths = cert_paths()
    if not paths:
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(*paths)
    return ctx


def reload_into(ctx: ssl.SSLContext) -> bool:
    """Swap a renewed certificate into a running server's context. New
    connections use it straight away; existing ones are unaffected."""
    paths = cert_paths()
    if not paths:
        return False
    try:
        ctx.load_cert_chain(*paths)
        return True
    except Exception as e:
        log.warning("couldn't reload certificate: %s", e)
        return False


def cert_mtime() -> float:
    paths = cert_paths()
    return os.path.getmtime(paths[0]) if paths else 0.0
