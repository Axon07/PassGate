"""Entry point: runs the guest portal, the admin dashboard and the
background sync loop in one process."""
import asyncio
import logging
import os

import uvicorn

from . import access, admin, billing, db, portal
from .util import secret_key

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("passgate")

PORTAL_PORT = int(os.environ.get("PORTAL_PORT", "80"))
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "8081"))
SYNC_SECONDS = int(os.environ.get("SYNC_SECONDS", "30"))
STRIPE_POLL_SECONDS = int(os.environ.get("STRIPE_POLL_SECONDS", "300"))


async def background():
    """Every 30 s: expire passes and push the allow-list to the router (this
    also restores it after a router reboot). Every 5 min: check Stripe."""
    loop = asyncio.get_running_loop()
    last_stripe = 0.0
    while True:
        try:
            if db.get("setup_complete") == "1":
                await loop.run_in_executor(None, access.sync)
                if loop.time() - last_stripe > STRIPE_POLL_SECONDS:
                    last_stripe = loop.time()
                    await loop.run_in_executor(None, billing.reconcile)
        except Exception:
            log.exception("background loop error")
        await asyncio.sleep(SYNC_SECONDS)


async def serve():
    db.init()
    secret = secret_key()
    servers = [
        uvicorn.Server(uvicorn.Config(portal.build(secret), host="0.0.0.0", port=PORTAL_PORT,
                                      proxy_headers=False, log_level="warning")),
        uvicorn.Server(uvicorn.Config(admin.build(secret), host="0.0.0.0", port=ADMIN_PORT,
                                      proxy_headers=False, log_level="warning")),
    ]
    log.info("PassGate: portal on :%d, admin on :%d", PORTAL_PORT, ADMIN_PORT)
    if db.get("setup_complete") != "1":
        log.info("First run: open http://<this-server>:%d/setup to configure.", ADMIN_PORT)
    await asyncio.gather(*(s.serve() for s in servers), background())


def run():
    asyncio.run(serve())


if __name__ == "__main__":
    run()
