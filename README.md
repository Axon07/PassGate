<div align="center">

<img src="docs/icon.png" width="96" alt="PassGate logo">

# PassGate

**Sell Wi-Fi passes with Stripe. Self-hosted, open source, and it works with the access points you already have.**

A captive portal for campgrounds, lakes, cabins, RV parks, marinas, and anywhere else you share an internet connection with paying guests.

[![CI](https://github.com/Axon07/Passgate/actions/workflows/ci.yml/badge.svg)](https://github.com/Axon07/Passgate/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Axon07/Passgate?include_prereleases&color=1f6f66)](https://github.com/Axon07/Passgate/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-1f6f66.svg)](LICENSE)
![Python 3.13](https://img.shields.io/badge/python-3.13-3776AB?logo=python&logoColor=white)
[![Docker image](https://img.shields.io/badge/image-ghcr.io%2Faxon07%2Fpassgate-2496ED?logo=docker&logoColor=white)](https://github.com/Axon07/Passgate/pkgs/container/passgate)
![Stripe](https://img.shields.io/badge/payments-Stripe-635BFF?logo=stripe&logoColor=white)
![OPNsense](https://img.shields.io/badge/router-OPNsense-D94F00?logo=opnsense&logoColor=white)
![OpenWrt](https://img.shields.io/badge/router-OpenWrt-00B5E2?logo=openwrt&logoColor=white)

[Features](#-features) · [How it works](#-how-it-works) · [Quick start](#-quick-start) · [OPNsense guide](docs/opnsense-setup.md) · [OpenWrt guide](docs/openwrt-setup.md) · [FAQ](#-faq)

<br>

<img src="docs/screenshots/portal.png" width="250" alt="Guest picking a pass"> &nbsp;
<img src="docs/screenshots/account.png" width="250" alt="Guest account with devices and extra slots"> &nbsp;
<img src="docs/screenshots/join-code.png" width="250" alt="Join code for a family member's device">

</div>

---

Guests join your open Wi-Fi, the sign-in page pops up, they pick a **day, weekend or monthly pass**, pay by card, and they're online. **Nobody has to be on site or hand out codes**, though you can print vouchers when you want to. Money goes straight to your Stripe account with no platform fee, and everything runs in one small Docker container next to your **OPNsense** or **OpenWrt** router.

## ✨ Features

<table>
<tr>
<td width="50%" valign="top">

### 💳 Selling access
- **Passes you design:** hours, days, "until end of season", or **monthly subscriptions** that stop billing on the last day of your season
- **Passes stack:** a day pass bought on top of a weekend pass adds time
- **Extra device slots** at checkout or later, **prorated** for the time left
- **Optional volume discount:** percent or dollar amount off each further slot
- **Vouchers** when you want them: printable codes for comps, bookings or pre-paid cards, timed from first use, with a use-by date and several devices per code
- **Free basic tier:** an always-available slow connection for messages and email, optionally a few minutes a day, with your passes as the upgrade
- **Free promotions:** open it up for a long weekend with a one-tap "Connect free" button and your own message, then a thank-you note pointing to the paid plans
- Refunds and cancellations from your Stripe dashboard are picked up automatically

</td>
<td width="50%" valign="top">

### 📱 Guests and devices
- **Email accounts** with a per-plan device limit
- Guests add their laptop by logging in on it, and **swap devices themselves**
- **Join codes:** a parent makes a code and the kids' phones join the pass, with no password or card needed
- **TVs and consoles** added by MAC address
- Access **follows each device** when its IP changes

</td>
</tr>
<tr>
<td valign="top">

### 🛠️ For the owner
- **First-run setup wizard**, then everything is changed in the browser
- Dashboard, guest lookup, comp passes, password resets, blocking
- **Per-guest device limits** and an **always-online** list for your own gear
- **Swap cooldown** so one login can't be passed around the campsite
- Optional **speed tiers** per plan through OPNsense's shaper

</td>
<td valign="top">

### 🧱 Built for the middle of nowhere
- **Works behind any ISP, CGNAT included:** no port forwarding or webhooks needed
- **Fails closed:** if the server is off, guests get nothing (not free internet)
- **Open-source routers:** OPNsense or OpenWrt does the blocking
- **Any access points:** UniFi, TP-Link, OpenWrt, HaLow bridges and others
- One Python process + SQLite. Upgrades migrate the database on start
- **Built-in HTTPS:** free Let's Encrypt certificates via DNS challenge (Cloudflare and 150+ providers), renewed automatically, even behind CGNAT
- MIT licensed. Your data stays on your hardware

</td>
</tr>
</table>

## 🧭 How it works

```mermaid
flowchart LR
    P["📱 Guest phone"] -- open SSID --> AP["Access points<br/>(any brand)"]
    AP -- guest VLAN --> FW["Router<br/>(OPNsense or OpenWrt)"]
    FW -- internet --> NET(("🌐 ISP"))
    FW -. "unpaid web traffic<br/>redirected" .-> PG["PassGate<br/>(Docker)"]
    PG -- "API: let this device through" --> FW
    PG -- "checkout & status" --> ST["💳 Stripe"]
```

1. A new phone joins the open network. The router blocks it and sends its web traffic to PassGate, so the **"Sign in to network"** sheet pops up.
2. The guest picks a plan and makes an account. PassGate opens the gate for a few minutes so the phone can reach **Stripe Checkout**.
3. Payment succeeds, and PassGate adds the device to the router's allow-list. **The guest is online.**
4. Every 30 seconds PassGate re-syncs the allow-list. It follows devices when their IP changes, removes expired passes, and rebuilds the list after a router reboot. Every 5 minutes it asks Stripe about renewals, cancellations and checkouts that never came back.

PassGate only decides **who** gets through; the firewall enforces it. Guest traffic never passes through the container.

## 🖼️ Screenshots

<table>
<tr>
<td><img src="docs/screenshots/admin-dashboard.png" alt="Admin dashboard"><p align="center"><sub>Owner dashboard</sub></p></td>
<td><img src="docs/screenshots/admin-plans.png" alt="Plans and prices"><p align="center"><sub>Plans &amp; prices</sub></p></td>
</tr>
<tr>
<td><img src="docs/screenshots/admin-plan-edit.png" alt="Plan editor with extra device slots"><p align="center"><sub>Plan editor: extra slots and discounts</sub></p></td>
<td align="center"><img src="docs/screenshots/portal-dark.png" width="300" alt="Guest portal in dark mode"><p align="center"><sub>Guest portal follows the phone's dark mode</sub></p></td>
</tr>
</table>

## 🚀 Quick start

> **Try it first, no router needed:**
> ```bash
> docker run -d --name passgate-try -p 8090:80 -p 8453:443 -p 8091:8081 -v passgate-try:/data ghcr.io/axon07/passgate:latest
> ```
> Open `http://localhost:8091/setup`, then the guest page on port 8090. In dry-run mode payments work (use a Stripe test key) but nobody is actually let online.

**For real:**

1. **Set up the network** with the guide for your router:
   - **[OPNsense guide](docs/opnsense-setup.md)**: guest VLAN, alias, firewall and NAT rules, API key, and a DNS name for the portal.
   - **[OpenWrt guide](docs/openwrt-setup.md)**: a guest network, then one script on the router does the firewall side.
2. **Run the container** with its own IP on a network behind the router, not on the guest network. Both guides have plain Docker, Proxmox and Unraid examples. With OPNsense's services VLAN, set `parent` in `docker-compose.yml` to the host's VLAN 51 interface (e.g. `eth0.51`), then:

   ```bash
   mkdir passgate && cd passgate && curl -fsSLO https://raw.githubusercontent.com/Axon07/Passgate/main/docker-compose.yml && docker compose up -d
   ```

   The image (`ghcr.io/axon07/passgate`) is built for amd64 and arm64, so it runs on a Raspberry Pi too. **Unraid** users can add it from a template (see below). Plain Docker and Proxmox examples are in the guides.

3. **Open the admin page** on port 8081 (`/setup`; the guide shows the address). Set your network name, portal address and admin password.
4. **Set your prices** under *Plans & prices*. Three starter plans are created for you.
5. In **Settings**, add your Stripe key, choose your *Router* and fill in its connection details, then press **Test**.
6. Buy a pass on your phone with Stripe's test card `4242 4242 4242 4242`. When it works, swap in your live key. 🎉

<details>
<summary><b>Unraid</b></summary>

Until PassGate is listed in Community Applications, add its template by hand. In the Unraid terminal, run:

```bash
wget -qO /boot/config/plugins/dockerMan/templates-user/my-PassGate.xml https://raw.githubusercontent.com/Axon07/Passgate/main/unraid/passgate.xml
```

then **Docker → Add Container → Template: PassGate**. The defaults run in bridge mode for trying it out. For real use, give it its own address: **Network Type** `br0.51` and **Fixed IP** `10.51.0.2` for the OPNsense layout, or `Custom: br0` and a free lan IP (e.g. `192.168.1.20`) for OpenWrt.
</details>

**Updating:** pull the new image and recreate the container (`docker compose pull && docker compose up -d`, or *Check for updates* in Unraid). Your data stays in the data folder, and the database upgrades itself on start.

## 📚 Guide

<details>
<summary><b>💳 Stripe setup</b></summary>

- Use a **secret key**, or a restricted key with write access to Checkout Sessions, Customers, Products, Subscriptions, Subscription Items and Billing Portal, plus read access to Balance for the test button.
- **Nothing to create in Stripe.** PassGate sends prices with each checkout, so you manage prices only in PassGate.
- Turn on the **Customer portal** (Stripe → Settings → Billing → Customer portal) so guests can cancel or update their card. The *Manage billing* button uses it.
- **Webhooks are optional.** They only help if Stripe can reach your portal, for example through a Cloudflare Tunnel. Endpoint: `https://your-public-name/stripe/webhook`. Events: `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.paid`. Paste the signing secret into Settings.
- Refunds and cancellations happen in the Stripe dashboard. PassGate picks them up within 5 minutes.

</details>

<details>
<summary><b>📱 Devices, limits and swapping</b></summary>

**How a device joins an account.** Guests never type a MAC. When a page is opened, PassGate asks the router's ARP table which MAC is using that IP. The phone that pays becomes device 1, and logging in on another device adds it. The router filters by IP, and every 30 seconds PassGate maps each registered MAC to its current IP.

**Device limit** = devices included + extra slots bought. Included devices come from the best active pass, or from a custom limit you set on the guest's admin page. A custom limit of 5 plus one bought slot means 6 devices.

**Hitting the limit.** The account page lists devices with *Swap for this device* buttons. If someone pays from a new device while full, the payment still goes through and they get the swap choice straight away, with no cooldown that time.

**Swap cooldown** (Settings, default 60 min, 0 = off). After removing or swapping a device, guests wait before the next swap. That makes sharing one login by swapping back and forth impractical. You can lift it per guest.

**A device that's already on someone else's account:**

| How it's added | Result |
|---|---|
| Logging in on the device itself | Moves to the new account (e.g. a kid's phone moving to their own account) |
| A guest typing in its MAC | Refused, so nobody can take a device by copying its MAC |
| The owner typing in its MAC | Moved, with a note |

**Adding by MAC** (TVs, consoles, streaming sticks). Guests can do this from their account page (you can turn it off), and the owner can from the admin page. It counts toward the limit. The MAC must be the one the device uses *on this network*, because phones and some TVs use a private MAC per Wi-Fi network.

**Always online.** Admin → *Always online*. These MACs get internet with no account or pass.

</details>

<details>
<summary><b>➕ Extra device slots and discounts</b></summary>

Off by default, and set per plan: *Plans → Edit → Extra device slots*.

- **Price per extra device** covers the plan's full length, or each billing period for subscriptions, up to *Most extras per account*.
- **Discount on each further extra** (optional): percent or fixed amount off each additional slot, never below *Never cheaper than*. For example, $3 with 10% off makes $3, $2.70, $2.40.
- **At checkout** guests pick "+1 / +2 / +3 devices" under the plan, and Stripe shows it as a second line.
- **Later**, *Buy device slots* charges only for the time left. Halfway through a weekend pass, a $3 slot costs $1.50. Stripe's minimum card charge of $0.50 applies.
  - On one-time passes, extras last until the guest's current access ends.
  - On subscriptions, the prorated amount covers the rest of this period, then the slot is added to the subscription at full price. PassGate reads extras back from Stripe, so removing the item there removes the slot.

</details>

<details>
<summary><b>👨‍👩‍👧 Join codes for families</b></summary>

On the account page, *Connect a family member's device* makes a one-time code like `K7P-42Q`, valid for 24 hours. On the other device, the sign-in page has *Have a join code?*

The device takes one of the account's slots and shows *"Connected through pa•••@example.com's pass"*. It **never gets the account itself**: no billing, device list or password. Ten wrong codes in an hour locks that device out for a while. A full account refuses the code with a clear message.

</details>

<details>
<summary><b>🔒 HTTPS</b></summary>

Card details never pass through PassGate, since guests enter them on Stripe's page. But emails, passwords and login cookies typed into the portal cross an open Wi-Fi network unencrypted unless the portal uses HTTPS.

Admin → **HTTPS** → *Free certificate from Let's Encrypt*: enter a name in a domain you own (e.g. `wifi.yourdomain.com`), an email, and a Cloudflare API token, then **Get certificate now**. PassGate proves you own the domain with a DNS challenge, so the portal never has to be reachable from the internet. It renews automatically. Other DNS providers work through the advanced option, and you can upload your own certificate files instead. Full steps are in [section 10 of the network guide](docs/opnsense-setup.md#10-https-for-the-portal-recommended).

</details>

<details>
<summary><b>🎉 Free promotions</b></summary>

Admin → *Promotions*. Set a start and end time, a headline and a message (for example *"Free Wi-Fi this long weekend! Like it? The monthly plan is $25"*). During the promotion the sign-in page shows a **Connect free** button: one tap, no account or card, online until the promotion ends. Paid plans are shown right underneath, and paying guests aren't affected.

When it ends (or you press *End it now*), free devices go back to the sign-in page, which shows them your thank-you note for a set number of days. Setting new dates starts a fresh promotion. Consider a per-device speed cap on your router before a busy free weekend.

</details>

<details>
<summary><b>🎟️ Vouchers</b></summary>

For access you hand out yourself: a comp for a neighbour, Wi-Fi included with a campsite booking, a prize, or pre-paid cards sold at the store. Admin → **Vouchers** → *Make vouchers*: a name, how many codes (up to 500), how long each lasts, devices per code, and an optional use-by date, speed tier and printed note.

- **Codes** look like `JBJ7-ZPUS`: no look-alike characters, and case and dashes don't matter.
- **Guests** enter them on the sign-in page under *Have a voucher or a join code?*. There's no account or card. The time starts at first use, and with a multi-device voucher, each device enters the same code.
- **Print** a sheet of cut-out cards with your network name, the code and short instructions, or **download a CSV**.
- **Track** each code: unused, in use (and until when), used up, expired or cancelled. Cancel a single code or a whole batch, or delete unused codes.
- Wrong codes are rate-limited per device, just like join codes. Vouchers never show up in the guest list or revenue figures.

</details>

<details>
<summary><b>🆓 Free basic tier</b></summary>

Admin → *Free Wi-Fi* → *Free basic tier*. The sign-in page then offers **Use free basic Wi-Fi** next to the paid plans: one tap, no account or card. Optionally limit it to a number of minutes per device per day (it resets at midnight).

Free devices go into the normal allow-list **and** a second router list (`free_clients`) that your router holds to a low speed, e.g. 2 Mbit/s. On OpenWrt the setup script creates that limit for you. On OPNsense it's two shaper rules ([guide](docs/opnsense-setup.md#8-speed-limits-optional-recommended)). A device that buys a pass, is on the always-online list, or is in the middle of paying always gets full speed.

The daily limit is per device, and someone determined can change their device's MAC address to get more. That's why the tier should be slow rather than relied on as a hard cap.

</details>

<details>
<summary><b>🐢 Speed limits</b></summary>

Speeds are enforced by the router. PassGate can't do it because guest traffic never passes through it.

- **One cap for everyone:** no PassGate setup needed. OPNsense: a shaper pipe with a per-IP mask ([guide](docs/opnsense-setup.md#8-speed-limits-optional-recommended)). OpenWrt: the nft-qos package ([guide](docs/openwrt-setup.md#7-speed-limits)).
- **Different speeds per plan** (OPNsense): in Settings → *Speed tiers*, list tiers as `Name = alias` (e.g. `Basic = paid_basic`, `Premium = paid_premium`) and pick a tier on each plan. PassGate puts each paid device in the main alias (internet on/off) **and** its tier alias, which your shaper rules match. Limits are per device.

</details>

<details>
<summary><b>⚙️ Ports, data and other details</b></summary>

| Port | What | Who should reach it |
|------|------|---------------------|
| `80` | Guest portal over HTTP (captive detection, then redirect to HTTPS when it's on), `/stripe/webhook`, `/api/captive` | guests |
| `443` | Guest portal over HTTPS, once a certificate is set up | guests |
| `8081` | Admin dashboard | only you: from your lan (OpenWrt layout) or through the OPNsense WAN port-forward. It refuses the guest subnet. |

- **Data** lives in `/data/passgate.db` (SQLite). Back up that folder.
- **Payment window:** unpaid devices get a few minutes of internet (default 15) to reach Stripe's page, limited to a few tries per device per day.
- **Time zone and season end** are in Settings. Subscriptions created while a season end is set get `cancel_at` set to that date in Stripe.

</details>

## ❓ FAQ

<details>
<summary><b>Do I need Ubiquiti, or any particular access points?</b></summary>

No. Any access point that can put guests on a VLAN works: UniFi, TP-Link Omada, OpenWrt, MikroTik, long-range point-to-multipoint links, HaLow bridges. Turn off the AP's own guest portal and turn on client isolation.
</details>

<details>
<summary><b>Does it work if my ISP uses CGNAT, or I can't open ports?</b></summary>

Yes. Many ISPs put customers behind CGNAT, so nothing on the internet can reach your server. PassGate only makes outgoing calls to Stripe and checks payment status itself, so you don't need webhooks or port forwarding.
</details>

<details>
<summary><b>Can people get around it?</b></summary>

MAC addresses are visible over the air and can be copied. That's true of **every** open-network captive portal, commercial ones included. Someone who copies a paying guest's MAC can use their pass while that guest is offline. Client isolation on your APs and a short always-online list help. The complete fix is per-user Wi-Fi credentials (WPA2/3-Enterprise), which is outside PassGate's scope.
</details>

<details>
<summary><b>Can guests use Apple Pay or Google Pay?</b></summary>

Usually not inside the pop-up sign-in sheet on phones, which is a limitation of those mini-browsers. Cards always work, and guests can open the portal address in their normal browser to use wallet payments.
</details>

<details>
<summary><b>Which routers work? What about pfSense or MikroTik?</b></summary>

PassGate controls **OPNsense** (through its API) and **OpenWrt 22.03+** (through a small helper that a setup script installs). Both are open source and run on cheap hardware. Router support lives in [`app/gateway.py`](app/gateway.py): a driver needs list / allow / revoke on an allow-list, plus an IP→MAC lookup. Pull requests for more drivers, such as pfSense or MikroTik, are very welcome.
</details>

<details>
<summary><b>Is reselling internet allowed?</b></summary>

That depends on your internet plan and where you are. Check that your provider's terms allow resale. Some providers and satellite services require a business plan, and some regions require registering as a reseller.
</details>

## 🧑‍💻 Development

```bash
pip install -r requirements.txt pytest && pytest -q
```

The test suite runs the full guest and owner flows against a fake Stripe and a dry-run router, and the OpenWrt driver against a fake router.

| File | What it does |
|---|---|
| `app/portal.py` | Guest pages: plans, checkout, account, join codes |
| `app/admin.py` | Owner dashboard and settings |
| `app/billing.py` | Stripe checkout, fulfilment, subscription sync |
| `app/extras.py` | Extra device slot pricing, discounts, proration |
| `app/access.py` | Who's allowed online, devices, invites, the sync loop |
| `app/gateway.py` | Router drivers (OPNsense, OpenWrt, dry-run) |
| `openwrt/setup.sh` | Router-side setup for OpenWrt: firewall set, rules, restricted login |
| `app/db.py` | SQLite schema, settings, migrations |

## 🤝 Contributing

Issues and pull requests are welcome, especially new router drivers, translations, and reports from real deployments. Please include a test for any change in behaviour.

## 📄 License

[MIT](LICENSE) © 2026 PassGate contributors
