# Changelog

## Unreleased

**New**
- **OpenWrt support.** PassGate can now control an OpenWrt router (22.03 or newer) as well as OPNsense. One script on the router (`openwrt/setup.sh`) adds the allow-list, firewall rules and a login that can only change that list. Choose *Router: OpenWrt* in Settings. Guide: `docs/openwrt-setup.md`.
- Settings only show the connection fields for the router you picked.

## v0.2.0

**New**
- **Built-in HTTPS.** Free Let's Encrypt certificates via DNS challenge (Cloudflare in one field, or any of 150+ providers), renewed automatically with no restart. Works behind CGNAT since nothing needs to be reachable from the internet. Or upload your own certificate files. Plain-HTTP visits are redirected to HTTPS and login cookies are marked Secure. New admin page: *HTTPS*.
- **Free-access promotions.** Schedule a free period (e.g. a long weekend): the sign-in page shows your message and a one-tap *Connect free* button (no account or card), with the paid plans underneath. Afterwards, those devices see your thank-you note. New admin page: *Promotions*.

**Upgrading:** pull the new image and recreate the container; the database updates itself. If you want HTTPS, also allow guests to reach port 443 on the portal (network guide, section 5) and see section 10.

## v0.1.0 — first public release

PassGate is a self-hosted captive portal that sells Wi-Fi access through Stripe.

**Selling access**
- Plans you define: hours, days, "until end of season", or recurring subscriptions that stop billing at season end
- One-time passes stack: extra time starts when the current pass ends
- Extra device slots at checkout or later, prorated for the time left, with optional per-slot discounts
- Works without Stripe webhooks (polls Stripe), so it runs behind CGNAT with no open ports

**Guests and devices**
- Email accounts with a per-plan device limit; guests add and swap their own devices
- Join codes so family members' devices can use a pass without the account password
- Add TVs and consoles by MAC address
- Access follows devices across IP changes

**For the owner**
- First-run setup wizard and browser-based admin: plans, guests, comp passes, password resets, blocking, activity log
- Per-guest device limits, swap cooldown, always-online MAC list
- Optional speed tiers per plan through OPNsense's traffic shaper

**Platform**
- OPNsense driver (firewall aliases via the API); dry-run mode for trying it out
- Single Python process + SQLite; multi-arch Docker image (amd64, arm64) on GHCR; Unraid template
