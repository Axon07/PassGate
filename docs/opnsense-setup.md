# Network setup: OPNsense + Docker + any access points

This is the reference layout. Addresses are examples, so change them to suit you, but keep the three
networks separate.

| Network | VLAN | Subnet | What lives there |
|---|---|---|---|
| Your existing LAN | (untagged) | e.g. 192.168.1.0/24 | your ISP router, your own devices |
| **GUEST** | 50 | 10.50.0.0/22 (≈1000 guests) | open SSID on every access point |
| **SERVICES** | 51 | 10.51.0.0/29 | the PassGate container (10.51.0.2) |

```
ISP ── your router (192.168.1.1) ── LAN ── OPNsense WAN (192.168.1.2)
                                           OPNsense GUEST    10.50.0.1  ── VLAN 50 ── access points
                                           OPNsense SERVICES 10.51.0.1  ── VLAN 51 ── PassGate 10.51.0.2
```

OPNsense can also be your main router, with its WAN plugged straight into the ISP. The guest and
services side is the same either way.

**Why the portal gets its own little network:** if it sat on the guest VLAN, the redirected web
requests would come back by a different path and phones would ignore them. With the portal behind
OPNsense, everything goes through the firewall both ways and the portal sees each guest's real IP.

---

## 1. Hardware: where OPNsense and PassGate run

You need two things, and they can be on the same machine or separate ones:

- **OPNsense** on a small dedicated box (any x86 mini PC with two NICs), or as a virtual machine on
  Proxmox, ESXi, Hyper-V, Unraid, TrueNAS or similar. 2 CPU cores, 2–4 GB RAM and 20 GB of disk is plenty.
- **PassGate** in Docker on any Linux host that can put a container on VLAN 51: the same hypervisor,
  a Raspberry Pi, a NAS, or a small VM.

**OPNsense network interfaces**, in this order:

| Interface | Connects to | Becomes |
|---|---|---|
| 1st | your existing LAN (or straight to the ISP) | **WAN** |
| 2nd | VLAN 50 | **GUEST** |
| 3rd | VLAN 51 | **SERVICES** |

On a physical box with fewer ports, use one port as a VLAN trunk and create the GUEST and SERVICES
VLANs in OPNsense (*Interfaces → Other Types → VLAN*). In a VM, give it one virtual NIC per network.
On virtual NICs (virtio/vmxnet), disable hardware checksum offload under *Interfaces → Settings*.

**Switch ports:** the port to the PassGate/OPNsense host carries VLANs 50 and 51 **tagged**. Ports to
access points carry VLAN 50 (tagged, or untagged on an access port). If OPNsense and PassGate are on
the same host, VLAN 51 can stay internal to it.

**PassGate's container** needs an address on VLAN 51, which is normally a Docker `macvlan` network on
the host's VLAN 51 interface. `docker-compose.yml` sets this up: change `parent` to that interface
(e.g. `eth0.51`), and adjust the subnet and IP if you changed them.

Set OPNsense and the container to **start automatically** after a reboot.

<details>
<summary><b>Example: plain Docker on any Linux host</b></summary>

With the host's VLAN 51 interface as `eth0.51` (change it to yours):

```bash
docker network create -d macvlan --subnet 10.51.0.0/29 --gateway 10.51.0.1 -o parent=eth0.51 passgate-net && docker build -t passgate:latest . && docker run -d --name passgate --restart unless-stopped --network passgate-net --ip 10.51.0.2 -v passgate-data:/data passgate:latest && docker logs passgate
```

Note that with `macvlan`, the host itself can't reach the container's IP. That's normal; you'll reach
the admin page through OPNsense (section 5).
</details>

<details>
<summary><b>Example: Proxmox</b></summary>

1. Make the Linux bridge VLAN-aware (*node → System → Network → vmbr0 → VLAN aware*).
2. OPNsense VM: three network devices on `vmbr0`, the 2nd with VLAN tag **50** and the 3rd with tag **51**.
3. PassGate: a small Debian VM or LXC with Docker, its network device on `vmbr0` with VLAN tag **51**,
   static IP `10.51.0.2/29`, gateway `10.51.0.1`. No macvlan needed, since the VM itself sits on
   VLAN 51. Inside it:

   ```bash
   docker build -t passgate:latest . && docker run -d --name passgate --restart unless-stopped -p 80:80 -p 8081:8081 -v passgate-data:/data passgate:latest
   ```
</details>

<details>
<summary><b>Example: everything on one Unraid server</b></summary>

1. **Settings → Network Settings** (the array may need stopping): *Enable VLANs*, add **VLAN 50** and
   **VLAN 51**. You get `br0.50` and `br0.51`.
2. **Settings → Docker** (stop Docker first): enable custom networks for `br0.50`/`br0.51` if listed,
   then start Docker again.
3. **VMs → Add VM** for OPNsense with three network cards: `br0` (WAN), `br0.50` (GUEST),
   `br0.51` (SERVICES). Turn on *autostart*.
4. Copy the PassGate folder to `appdata/passgate-src` over the network share, then in the terminal:

   ```bash
   cd /mnt/user/appdata/passgate-src && docker build -t passgate:latest . && docker run -d --name passgate --restart unless-stopped --network br0.51 --ip 10.51.0.2 -v /mnt/user/appdata/passgate:/data passgate:latest && docker logs passgate
   ```
</details>


## 2. OPNsense interfaces and DHCP

- **WAN**: DHCP from your existing router, with a static lease (e.g. 192.168.1.2). If WAN is on your
  LAN rather than the ISP directly, untick **Block private networks** (your LAN is private).
- **GUEST**: static `10.50.0.1/22`. **Services → DHCPv4 (or Kea/Dnsmasq) → GUEST**: range
  `10.50.0.10 – 10.50.3.250`, lease time **86400** (24 h). Long leases keep IPs stable.
- **SERVICES**: static `10.51.0.1/29`. No DHCP needed.
- **Services → Unbound DNS**: enabled on all interfaces. Add a **host override** for the portal name,
  e.g. `wifi.yourdomain.ca → 10.51.0.2`. A real domain you own is safest for Stripe's return link. A
  local-only name like `wifi.lake.lan` normally works too, since the guest's phone does the redirect.

## 3. Aliases (Firewall → Aliases)

| Name | Type | Content |
|---|---|---|
| `paid_clients` | **External (advanced)** | *(empty: PassGate fills it)* |
| `portal_host` | Host(s) | `10.51.0.2` |
| `private_nets` | Network(s) | `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` |
| `paid_basic`, `paid_premium`… | External (advanced) | *only if you use speed tiers (section 8)* |

## 4. NAT: send unpaid web traffic to the portal

**Firewall → NAT → Port Forward → Add**

- Interface: **GUEST** · Protocol: **TCP**
- Source: **`paid_clients`** with **Invert** ticked (i.e. *not* paid)
- Destination: any · Destination port: **HTTP (80)**
- Redirect target IP: **`portal_host`** · Redirect target port: **80**
- Filter rule association: **None** (rule 2 below covers it)
- Description: `PassGate captive redirect`

## 5. Firewall rules

**GUEST** (top to bottom):

| # | Action | Proto | Source | Destination | Port | Why |
|---|---|---|---|---|---|---|
| 1 | Pass | TCP/UDP | GUEST net | GUEST address | 53 | DNS for everyone |
| 2 | Pass | TCP | GUEST net | `portal_host` | 80 | the portal (incl. redirected traffic) |
| 3 | Pass | any | **`paid_clients`** | **`private_nets` (Invert)** | any | paid guests → internet only |
| 4 | Block | any | GUEST net | any | any | everything else (log it while testing) |

**SERVICES**:

| # | Action | Proto | Source | Destination | Port | Why |
|---|---|---|---|---|---|---|
| 1 | Pass | TCP | `portal_host` | SERVICES address | 443 | PassGate → OPNsense API |
| 2 | Pass | TCP/UDP | `portal_host` | SERVICES address | 53 | DNS |
| 3 | Pass | any | `portal_host` | `private_nets` (Invert) | any | PassGate → Stripe |
| 4 | Block | any | SERVICES net | any | any | |

**WAN** (to reach the admin page from your LAN):

- **Firewall → NAT → Port Forward**: Interface **WAN**, TCP, Source **your LAN subnet** (e.g.
  192.168.1.0/24), Destination **WAN address** port **8081** → redirect to `portal_host` port **8081**,
  filter rule association **Add associated filter rule**.
- Admin page is then at `http://192.168.1.2:8081` (the OPNsense WAN IP).

Guests can't reach your LAN (rule 3 excludes private networks, rule 4 blocks the rest), and they can't
reach the admin page (no rule allows it, and PassGate refuses the guest subnet as well).

## 6. API key for PassGate

1. **System → Access → Users → Add** `passgate` (random password; it never logs in to the GUI).
2. Privileges. In the privilege list, filter and tick:
   - anything named **Firewall: Alias** (alias editing and the alias *util* endpoints)
   - **Diagnostics: ARP Table**
   - **Diagnostics: pf States** / **Firewall: States** (whichever your version has; used to cut off
     expired guests immediately)

   Names shift a little between OPNsense releases. If the *Test* button in PassGate shows `403`, add the
   privilege matching the endpoint in the error. As a quick start you can give the user the
   *GUI - All pages* privilege and narrow it later.
3. On that user, **API keys → +**. A file downloads with `key=` and `secret=`. Paste both into PassGate
   **Settings**, set **OPNsense address** to `https://10.51.0.1`, **Router** to OPNsense, and press
   **Test**.

## 7. Access points (any brand)

All they need to do is put guests on VLAN 50, with **no portal of their own** and **client isolation on**.

- **UniFi**: *Networks → New*. Type **Third-party Gateway** (older versions: *VLAN only*), VLAN ID
  **50**. *WiFi → New*: open (no password), network = that VLAN, **Client Device Isolation** on,
  hotspot/guest portal **off**. Your existing UniFi controller keeps managing the APs as usual.
- **OpenWrt / other APs**: create an open SSID bridged to VLAN 50, enable AP isolation.
- **Long-range / point-to-multipoint / HaLow links**: run them as transparent bridges that carry
  VLAN 50 (tagged) to the remote APs. Anything that bridges layer 2 works.

## 8. Speed limits (optional, recommended)

Guest traffic is NAT'd on WAN, so shape on the **GUEST** interface, where real guest IPs are visible.
*Firewall → Shaper*:

**One cap for everyone** (no PassGate setup):

| Pipes | Bandwidth | Mask |
|---|---|---|
| `guest-down` | e.g. 15 Mbit/s | **destination** (each guest IP gets its own 15) |
| `guest-up` | e.g. 5 Mbit/s | **source** |

| Rules | Interface | Direction | Source | Destination | Target |
|---|---|---|---|---|---|
| seq 100 | GUEST | out | any | GUEST net | `guest-down` |
| seq 110 | GUEST | in | GUEST net | any | `guest-up` |

(Download leaves OPNsense *out* of the GUEST interface toward the guest; upload comes *in*.)

**Tiers per plan.** Create one more **External (advanced)** alias per tier (e.g. `paid_basic`,
`paid_premium`), list them in PassGate Settings → *Speed tiers*, and choose a tier on each plan. Add
a pipe pair per tier and put the tier rules **before** the catch-all rules above:

| Rules | Interface | Direction | Source | Destination | Target |
|---|---|---|---|---|---|
| seq 10 | GUEST | out | any | `paid_premium` | `premium-down` (e.g. 50 Mbit/s, mask destination) |
| seq 11 | GUEST | in | `paid_premium` | any | `premium-up` (mask source) |
| seq 20 | GUEST | out | any | `paid_basic` | `basic-down` |
| seq 21 | GUEST | in | `paid_basic` | any | `basic-up` |
| seq 100/110 | (catch-all from above) | | | | |

Recent OPNsense releases accept aliases in shaper rules. If yours doesn't offer them in the
source/destination fields, upgrade, or stick to the single cap for everyone. Firewall rules don't
change: internet on/off is still decided by `paid_clients` only.

Caps are per device. Added together, the caps for everyone online at once will be more than your
connection can carry, which is fine. The point is that one guest can't take it all.

## 9. Testing checklist

1. Phone joins the SSID → gets a 10.50.x.x address → "Sign in" sheet appears with your plans.
2. With a Stripe **test** key, buy a Day Pass with card `4242 4242 4242 4242`, any future date, any CVC.
3. After payment: **Firewall → Diagnostics → Aliases → paid_clients** shows the phone's IP and the phone
   browses normally.
4. Log in on a second device with the same email → it connects. A third (on a 2-device plan) asks
   which one to swap.
5. In PassGate admin, end the pass → within ~30 s the phone loses internet and gets the portal again.
6. Turn the PassGate container off → new guests get no internet (fail-closed). Turn OPNsense off
   → nobody on the guest SSID gets an address at all.
7. Reboot OPNsense → the alias comes back empty and PassGate refills it within 30 s.

Then switch Stripe to your **live** key and you're open for business.

## Optional extras

- **UPS** on the server and network gear, with OPNsense and the container set to start automatically,
  means everything comes back by itself after a power blip.
- **Webhooks** (instant subscription updates) need Stripe to reach `/stripe/webhook`. If your ISP uses
  CGNAT or you'd rather not open a port, that means something like a Cloudflare Tunnel. Without them, PassGate polls Stripe every
  5 minutes, which is fine.
