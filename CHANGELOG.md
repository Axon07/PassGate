# Changelog

## Unreleased

- Free-access promotions: scheduled free period with a one-tap "Connect free" button, custom message, and a thank-you note with the paid plans afterwards

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
