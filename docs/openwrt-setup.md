# Network setup: OpenWrt + Docker + any access points

PassGate can control an **OpenWrt** router (22.03 or newer) instead of OPNsense. A setup script does
the router side: it adds a firewall set that PassGate fills with paid guests, a handful of firewall
rules, and a login that is only allowed to change that set.

This is the reference layout. Change the addresses to suit you.

| Network | Subnet | What lives there |
|---|---|---|
| **lan** | e.g. 192.168.1.0/24 | your own devices, and PassGate with its own address (e.g. 192.168.1.20) |
| **guest** | 10.50.0.0/22 (≈1000 guests) | the open SSID: on the router's own radios, or a VLAN (e.g. 50) to your access points |
| **wan** | from your ISP | the internet |

```
ISP ── OpenWrt (wan)
       OpenWrt lan   192.168.1.1  ── your devices, Docker host, PassGate 192.168.1.20
       OpenWrt guest 10.50.0.1    ── open SSID / VLAN 50 ── access points
```

PassGate must **not** be on the guest network itself: redirected requests would come back by a different
path and phones would ignore them. On the lan (or any other zone that isn't guest), all guest traffic to
it goes through the router, and it sees each guest's real IP.

Guests can only reach PassGate's ports 80 and 443. They can't reach the rest of your lan, the admin page
(PassGate also refuses the guest subnet) or the router's own pages.

---

## 1. What you need

- **OpenWrt 22.03 or newer** (it uses firewall4/nftables). 23.05 and 24.10 are fine. Release images
  include LuCI, which brings everything the script needs. On a build without LuCI, the script tells you
  which packages to add.
- **A Docker host on the lan** for PassGate: Unraid, a Raspberry Pi, a NAS, a small VM. The image is
  built for amd64 and arm64.

## 2. Guest network (LuCI)

Skip this if you already have a guest network with its own firewall zone.

1. **Network → Interfaces → Add new interface**: name `guest`, protocol *Static address*, device:
   - access points on a VLAN: a VLAN device on your lan port or bridge with VLAN ID **50** (on newer
     routers, *Network → Interfaces → Devices → br-lan → Bridge VLAN filtering*; on others, a
     *VLAN (802.1q)* device)
   - router's own Wi-Fi only: leave the device empty for now and attach the SSID in step 4
2. IPv4 address `10.50.0.1`, netmask `255.255.252.0`.
3. **DHCP Server → Set up DHCP server**: start `10`, limit `1000`, lease time `24h` (long leases keep IPs
   stable). Under *IPv6 Settings*, set RA-Service and DHCPv6-Service to *disabled*: PassGate works with
   IPv4, and phones fall back to it at once.
4. **Firewall Settings**: create a new zone named `guest`. Don't add any forwarding to wan: the setup
   script would remove it anyway, because then nobody would need to pay.
5. **Router's own radios** (optional): *Network → Wireless → Add*, mode Access Point, ESSID of your
   choice, network `guest`, *Wireless Security* → No encryption, *Advanced* → **Isolate clients** on.

## 3. Run PassGate with its own address on the lan

Give the container its own IP so it can use ports 80 and 443 (your host's ports stay free).

<details>
<summary><b>Unraid</b></summary>

Add PassGate from its template (README → Unraid), then set **Network Type** to `Custom: br0` and
**Fixed IP address** to e.g. `192.168.1.20` (outside the router's DHCP range). If `Custom: br0` isn't
offered: *Settings → Docker*, stop Docker, switch to *Advanced View* and enable *IPv4 custom network on
interface br0*, then start Docker again.

The admin page is then at `http://192.168.1.20:8081`.
</details>

<details>
<summary><b>Plain Docker on any Linux host</b></summary>

With the host's lan interface as `eth0` (change it, the subnet and the IP to yours):

```bash
docker network create -d macvlan --subnet 192.168.1.0/24 --gateway 192.168.1.1 -o parent=eth0 passgate-lan && docker run -d --name passgate --restart unless-stopped --network passgate-lan --ip 192.168.1.20 -v passgate-data:/data ghcr.io/axon07/passgate:latest && docker logs passgate
```

Pick an IP outside the router's DHCP range. With `macvlan`, the host itself can't reach the
container's IP; other machines on your lan can.
</details>

<details>
<summary><b>A small VM or Proxmox LXC</b></summary>

Give the VM a static lan address (e.g. 192.168.1.20) and run PassGate with the standard ports:

```bash
docker run -d --name passgate --restart unless-stopped -p 80:80 -p 443:443 -p 8081:8081 -v passgate-data:/data ghcr.io/axon07/passgate:latest
```
</details>

Open the admin page (`http://192.168.1.20:8081/setup`) and finish the setup wizard. Set **Guest network**
to `10.50.0.0/22`.

## 4. Run the setup script on the router

SSH into the router (`ssh root@192.168.1.1`) and run this, with your own password and PassGate's IP.
`PORTAL_HOST` is optional: it adds a local DNS name for the portal (use a name in a domain you own if
you want HTTPS later).

```sh
wget -O /tmp/passgate-setup.sh https://raw.githubusercontent.com/Axon07/Passgate/main/openwrt/setup.sh && PASSWORD='choose-a-long-password' PORTAL_IP=192.168.1.20 PORTAL_HOST=wifi.yourdomain.com sh /tmp/passgate-setup.sh
```

If your zones aren't called `guest`, `lan` and `wan`, add `GUEST_ZONE=...`, `PORTAL_ZONE=...` or
`WAN_ZONE=...` before `sh`. The script is safe to run again, e.g. to change the password or IP.

**What it changes:**

| | |
|---|---|
| Firewall set `paid_clients` | the allow-list PassGate fills |
| Rules | guests may use the router's DNS and DHCP and reach PassGate on 80/443; guests in the set may reach the internet, but never private networks on the wan side |
| Redirect | web requests (port 80) from guests *not* in the set go to PassGate, so the sign-in page pops up |
| Guest zone | input and forward set to *reject*, and any guest → wan forwarding removed |
| `/usr/libexec/passgate-gate` | a small helper that adds or removes an IP in the set and reads the ARP table |
| rpcd login `passgate` | can run that helper and nothing else; it can't log in to LuCI or change any other setting |
| `/etc/sysupgrade.conf` | keeps the helper through firmware upgrades |
| dnsmasq | `PORTAL_HOST` → PassGate's IP, if you set it |

To undo it all: `sh /tmp/passgate-setup.sh remove` (download it again first if the router rebooted).

**Recommended:** install `conntrack` (`opkg update && opkg install conntrack`, or `apk add conntrack` on
newer releases) so that when a pass ends, open connections such as a video stream are cut immediately
rather than when they next reconnect.

## 5. Connect PassGate to the router

PassGate admin → **Settings**:

- **Portal address**: `http://wifi.yourdomain.com` (or `http://192.168.1.20` if you skipped `PORTAL_HOST`)
- **Router**: OpenWrt
- **OpenWrt address**: `http://192.168.1.1` (`https://` works too; leave *Verify certificate* off for the
  router's self-signed one)
- **OpenWrt user**: `passgate` · **OpenWrt password**: the `PASSWORD` from step 4
- **Allow-list name**: `paid_clients`

Press **Test Stripe & router connection**. It should say *Connected (passgate-gate 1)*, the number of
entries in the set, and how many devices the router sees.

## 6. Access points (any brand)

All they need to do is put guests on the guest VLAN, with **no portal of their own** and **client
isolation on**.

- **UniFi**: *Networks → New*, type **Third-party Gateway** (older versions: *VLAN only*), VLAN ID
  **50**. *WiFi → New*: open, network = that VLAN, **Client Device Isolation** on, guest portal **off**.
- **OpenWrt access points**: an open SSID bridged to VLAN 50, *Isolate clients* on.
- **Long-range / HaLow / point-to-multipoint links**: run them as transparent bridges that carry VLAN 50.

## 7. Speed limits

Speeds are enforced by the router, since guest traffic never passes through PassGate. On OpenWrt, a
simple per-device cap for the whole guest network can be set with the **nft-qos** package
(`luci-app-nft-qos`, *Network → QoS over Nftables*, static limit for `10.50.0.0/22`). PassGate's
per-plan *speed tiers* aren't wired up for OpenWrt yet: PassGate will fill extra sets if you list them,
but you'd have to write the matching shaping rules yourself.

## 8. Testing checklist

1. Phone joins the SSID → gets a 10.50.x.x address → the "Sign in" sheet appears with your plans.
2. With a Stripe **test** key, buy a Day Pass with card `4242 4242 4242 4242`, any future date, any CVC.
3. After payment, on the router: `nft list set inet fw4 paid_clients` shows the phone's IP, and the
   phone browses normally.
4. In PassGate admin, end the pass → within ~30 s the phone loses internet and gets the portal again.
5. Stop the PassGate container → new guests get no internet (fails closed).
6. Restart the firewall (`/etc/init.d/firewall restart`) or reboot the router → the set comes back
   empty and PassGate refills it within 30 s.

Then switch Stripe to your **live** key and you're open for business.

## 9. HTTPS for the portal (recommended)

Works the same as with OPNsense: see [section 10 of the OPNsense guide](opnsense-setup.md#10-https-for-the-portal-recommended).
The only OpenWrt-specific part is the local DNS name, which `PORTAL_HOST` in step 4 already set up.

## Good to know

- **Firewall restarts empty the set.** Any change saved in LuCI's firewall pages restarts it. Guests
  already connected keep their open connections, and PassGate refills the set within 30 seconds.
- **The helper is all PassGate can do on the router.** It accepts only a set name made of letters,
  digits and `_` and a plain IPv4 address, and only touches sets in the firewall4 table.
- **Older OpenWrt (21.02 and earlier, iptables)** isn't supported.
