"""Entry point: runs the guest portal (HTTP, and HTTPS once a certificate
exists), the admin dashboard and the background loop in one process."""
import asyncio
import logging
import os

import uvicorn

from . import access, admin, billing, db, portal, tls
from .util import secret_key

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("passgate")

PORTAL_PORT = int(os.environ.get("PORTAL_PORT", "80"))
HTTPS_PORT = int(os.environ.get("HTTPS_PORT", "443"))
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "8081"))
SYNC_SECONDS = int(os.environ.get("SYNC_SECONDS", "30"))
STRIPE_POLL_SECONDS = int(os.environ.get("STRIPE_POLL_SECONDS", "300"))


class HttpsManager:
    """Starts the HTTPS listener as soon as a certificate is available,
    swaps in renewed certificates, and stops it if HTTPS is turned off."""

    def __init__(self, app):
        self.app = app
        self.server = None
        self.task = None
        self.ctx = None
        self.mtime = 0.0

    async def tick(self):
        loop = asyncio.get_running_loop()
        if tls.due_for_check():
            await loop.run_in_executor(None, tls.obtain_or_renew)
        running = self.task is not None and not self.task.done()
        if tls.active() and not running:
            self.ctx = tls.ssl_context()
            ctx = self.ctx
            cfg = uvicorn.Config(self.app, host="0.0.0.0", port=HTTPS_PORT, proxy_headers=False,
                                 log_level="warning", ssl_context_factory=lambda c, default: ctx)
            self.server = uvicorn.Server(cfg)
            self.task = asyncio.create_task(self.server.serve())
            self.mtime = tls.cert_mtime()
            log.info("HTTPS: portal on :%d for %s", HTTPS_PORT, tls.portal_host() or "(certificate names)")
        elif running and not tls.active():
            self.server.should_exit = True
            log.info("HTTPS turned off")
        elif running and tls.cert_mtime() != self.mtime:
            if tls.reload_into(self.ctx):
                self.mtime = tls.cert_mtime()
                log.info("HTTPS: renewed certificate loaded")


async def background(https: HttpsManager):
    """Every 30 s: expire passes and push the allow-list to the router (this
    also restores it after a router reboot) and look after HTTPS.
    Every 5 min: check Stripe."""
    loop = asyncio.get_running_loop()
    last_stripe = 0.0
    while True:
        try:
            if db.get("setup_complete") == "1":
                await loop.run_in_executor(None, access.sync)
                if loop.time() - last_stripe > STRIPE_POLL_SECONDS:
                    last_stripe = loop.time()
                    await loop.run_in_executor(None, billing.reconcile)
            await https.tick()
        except Exception:
            log.exception("background loop error")
        await asyncio.sleep(SYNC_SECONDS)


async def serve():
    db.init()
    secret = secret_key()
    portal_app = portal.build(secret)
    servers = [
        uvicorn.Server(uvicorn.Config(portal_app, host="0.0.0.0", port=PORTAL_PORT,
                                      proxy_headers=False, log_level="warning")),
        uvicorn.Server(uvicorn.Config(admin.build(secret), host="0.0.0.0", port=ADMIN_PORT,
                                      proxy_headers=False, log_level="warning")),
    ]
    log.info("PassGate: portal on :%d, admin on :%d", PORTAL_PORT, ADMIN_PORT)
    if db.get("setup_complete") != "1":
        log.info("First run: open http://<this-server>:%d/setup to configure.", ADMIN_PORT)
    await asyncio.gather(*(s.serve() for s in servers), background(HttpsManager(portal_app)))


def run():
    asyncio.run(serve())


if __name__ == "__main__":
    run()
