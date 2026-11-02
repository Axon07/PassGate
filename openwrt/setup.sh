#!/bin/sh
# PassGate setup for OpenWrt 22.03+ (firewall4 / nftables).
#
# Run on the router, as root, after the guest network exists:
#
#   PASSWORD='long-random-password' PORTAL_IP=192.168.1.20 sh setup.sh
#
# Settings (environment variables):
#   PASSWORD     required. PassGate logs in with this (Settings -> OpenWrt password).
#   PORTAL_IP    required. The PassGate container's IP address.
#   GUEST_ZONE   firewall zone of the guest Wi-Fi           (default: guest)
#   PORTAL_ZONE  firewall zone PassGate lives in            (default: lan)
#   WAN_ZONE     firewall zone of the internet uplink       (default: wan)
#   SET          name of the allow-list set                 (default: paid_clients)
#   PG_USER      login name for PassGate                    (default: passgate)
#   PORTAL_HOST  optional. A name like wifi.example.com; adds a local DNS record for it.
#
# Safe to run again (it replaces its own settings). To undo everything:
#   sh setup.sh remove

set -e
GUEST_ZONE="${GUEST_ZONE:-guest}"
PORTAL_ZONE="${PORTAL_ZONE:-lan}"
WAN_ZONE="${WAN_ZONE:-wan}"
SET="${SET:-paid_clients}"
PG_USER="${PG_USER:-passgate}"
HELPER=/usr/libexec/passgate-gate
ACL=/usr/share/rpcd/acl.d/passgate.json
SECTIONS="passgate_set passgate_dns passgate_dhcp passgate_api passgate_portal passgate_private passgate_paid passgate_redirect"

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

remove_all() {
	for s in $SECTIONS; do uci -q delete "firewall.$s" || true; done
	uci -q delete rpcd.passgate || true
	if [ -f /etc/passgate-dns ]; then
		uci -q del_list dhcp.@dnsmasq[0].address="$(cat /etc/passgate-dns)" || true
		rm -f /etc/passgate-dns
	fi
	rm -f "$HELPER" "$ACL"
	[ -f /etc/sysupgrade.conf ] && sed -i '\#passgate#d' /etc/sysupgrade.conf
	return 0
}

reload_all() {
	uci commit firewall
	uci commit rpcd
	uci commit dhcp
	/etc/init.d/rpcd restart
	/etc/init.d/firewall restart >/dev/null 2>&1 || /etc/init.d/firewall restart
	/etc/init.d/dnsmasq restart >/dev/null 2>&1 || true
}

if [ "$1" = remove ]; then
	remove_all
	reload_all
	say "PassGate removed. If you deleted the guest -> $WAN_ZONE forwarding during setup, add it back"
	say "in LuCI (Network -> Firewall -> Zones) if you want the guest network open again."
	exit 0
fi

# --- Checks -----------------------------------------------------------------
[ "$(id -u)" = 0 ] || die "run this as root"
[ -n "$PASSWORD" ] || die "set PASSWORD='...' (PassGate uses it to log in to the router)"
printf '%s' "$PORTAL_IP" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || die "set PORTAL_IP to the PassGate container's IPv4 address"
printf '%s' "$SET" | grep -Eq '^[A-Za-z0-9_]{1,31}$' || die "SET may only use letters, digits and _"
command -v fw4 >/dev/null 2>&1 || die "this needs OpenWrt 22.03 or newer (firewall4). Older releases aren't supported."
zone_section() {
	for z in $(uci show firewall | sed -n 's/^firewall\.\([^.=]*\)=zone$/\1/p'); do
		[ "$(uci -q get "firewall.$z.name")" = "$1" ] && { echo "$z"; return 0; }
	done
	return 1
}
zone_exists() { zone_section "$1" >/dev/null; }
zone_exists "$GUEST_ZONE" || die "no firewall zone named '$GUEST_ZONE'. Create the guest network first (see docs/openwrt-setup.md) or set GUEST_ZONE=..."
zone_exists "$PORTAL_ZONE" || die "no firewall zone named '$PORTAL_ZONE'. Set PORTAL_ZONE=... to the zone PassGate is in"
zone_exists "$WAN_ZONE" || die "no firewall zone named '$WAN_ZONE'. Set WAN_ZONE=..."
[ "$GUEST_ZONE" != "$PORTAL_ZONE" ] || die "PassGate must not be on the guest network itself (see the guide)"

missing=""
[ -x /usr/sbin/uhttpd ] || missing="$missing uhttpd"
[ -e /usr/lib/uhttpd_ubus.so ] || missing="$missing uhttpd-mod-ubus"
[ -e /usr/lib/rpcd/file.so ] || missing="$missing rpcd-mod-file"
if [ -n "$missing" ]; then
	if command -v apk >/dev/null 2>&1; then pm="apk update && apk add"; else pm="opkg update && opkg install"; fi
	die "missing packages:$missing. Install them with:  $pm$missing   (installing 'luci' also covers them)"
fi

# --- Helper: the one command PassGate may run --------------------------------
mkdir -p /usr/libexec
cat > "$HELPER" <<'EOF'
#!/bin/sh
# PassGate helper (installed by PassGate's openwrt/setup.sh).
# The PassGate login may run this and nothing else. It only touches
# nftables sets in the fw4 table and reads the ARP table.
#   passgate-gate list SET | allow SET IP | revoke SET IP [kill] | arp | version
# Strict checks: a set name is letters/digits/_, an IP is digits and dots only
# (so nothing else, not even a newline, can reach the nft command line).
set_ok() { case "$1" in ""|*[!A-Za-z0-9_]*) return 1 ;; esac; [ ${#1} -le 31 ]; }
ip_ok() {
	case "$1" in ""|*[!0-9.]*) return 1 ;; esac
	printf '%s' "$1" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$'
}
case "$1" in
version) echo "passgate-gate 1" ;;
arp) cat /proc/net/arp ;;
list)
	set_ok "$2" || { echo "bad set name" >&2; exit 2; }
	exec nft list set inet fw4 "$2" ;;
allow)
	set_ok "$2" && ip_ok "$3" || { echo "bad arguments" >&2; exit 2; }
	exec nft add element inet fw4 "$2" "{ $3 }" ;;
revoke)
	set_ok "$2" && ip_ok "$3" || { echo "bad arguments" >&2; exit 2; }
	nft delete element inet fw4 "$2" "{ $3 }" 2>/dev/null
	# Cut open connections too, if conntrack is installed (optional package).
	if [ "$4" = kill ] && command -v conntrack >/dev/null 2>&1; then
		conntrack -D -s "$3" >/dev/null 2>&1
		conntrack -D -d "$3" >/dev/null 2>&1
	fi
	exit 0 ;;
*) echo "usage: passgate-gate list|allow|revoke|arp|version" >&2; exit 2 ;;
esac
EOF
chmod 755 "$HELPER"

# --- Login for PassGate, allowed to run only the helper ----------------------
mkdir -p /usr/share/rpcd/acl.d
cat > "$ACL" <<EOF
{
	"passgate": {
		"description": "PassGate captive portal: may only run $HELPER",
		"write": {
			"ubus": { "file": [ "exec" ] },
			"file": { "$HELPER": [ "exec" ] }
		}
	}
}
EOF
HASH="$(uhttpd -m "$PASSWORD")"
[ -n "$HASH" ] || die "couldn't hash the password with uhttpd -m"
uci -q delete rpcd.passgate || true
uci set rpcd.passgate=login
uci set rpcd.passgate.username="$PG_USER"
uci set rpcd.passgate.password="$HASH"
uci add_list rpcd.passgate.read='passgate'
uci add_list rpcd.passgate.write='passgate'
[ -n "$(uci -q get uhttpd.main.ubus_prefix)" ] || { uci set uhttpd.main.ubus_prefix='/ubus'; uci commit uhttpd; /etc/init.d/uhttpd restart; }

# Keep the helper and its permission file across firmware upgrades (sysupgrade)
touch /etc/sysupgrade.conf
for f in "$HELPER" "$ACL"; do grep -qx "$f" /etc/sysupgrade.conf || echo "$f" >> /etc/sysupgrade.conf; done

# --- Firewall ----------------------------------------------------------------
for s in $SECTIONS; do uci -q delete "firewall.$s" || true; done

# The allow-list PassGate fills (empty after every firewall restart; PassGate refills it within 30 s)
uci set firewall.passgate_set=ipset
uci set firewall.passgate_set.name="$SET"
uci set firewall.passgate_set.family='ipv4'
uci add_list firewall.passgate_set.match='src_ip'

# Everyone on the guest network: DNS and DHCP from the router, and the portal itself
uci set firewall.passgate_dns=rule
uci set firewall.passgate_dns.name='PassGate: guest DNS'
uci set firewall.passgate_dns.src="$GUEST_ZONE"
uci set firewall.passgate_dns.proto='tcp udp'
uci set firewall.passgate_dns.dest_port='53'
uci set firewall.passgate_dns.target='ACCEPT'

uci set firewall.passgate_dhcp=rule
uci set firewall.passgate_dhcp.name='PassGate: guest DHCP'
uci set firewall.passgate_dhcp.src="$GUEST_ZONE"
uci set firewall.passgate_dhcp.family='ipv4'
uci set firewall.passgate_dhcp.proto='udp'
uci set firewall.passgate_dhcp.dest_port='67'
uci set firewall.passgate_dhcp.target='ACCEPT'

uci set firewall.passgate_portal=rule
uci set firewall.passgate_portal.name='PassGate: guests -> portal'
uci set firewall.passgate_portal.src="$GUEST_ZONE"
uci set firewall.passgate_portal.dest="$PORTAL_ZONE"
uci set firewall.passgate_portal.dest_ip="$PORTAL_IP"
uci set firewall.passgate_portal.proto='tcp'
uci set firewall.passgate_portal.dest_port='80 443'
uci set firewall.passgate_portal.target='ACCEPT'

# PassGate -> router web server (only matters if its zone blocks input)
uci set firewall.passgate_api=rule
uci set firewall.passgate_api.name='PassGate: portal -> router API'
uci set firewall.passgate_api.src="$PORTAL_ZONE"
uci set firewall.passgate_api.src_ip="$PORTAL_IP"
uci set firewall.passgate_api.proto='tcp'
uci set firewall.passgate_api.dest_port='80 443'
uci set firewall.passgate_api.target='ACCEPT'

# Paid guests: internet only, never private networks on the uplink side
uci set firewall.passgate_private=rule
uci set firewall.passgate_private.name='PassGate: guests never reach private networks'
uci set firewall.passgate_private.src="$GUEST_ZONE"
uci set firewall.passgate_private.dest="$WAN_ZONE"
uci set firewall.passgate_private.family='ipv4'
uci set firewall.passgate_private.proto='all'
uci add_list firewall.passgate_private.dest_ip='10.0.0.0/8'
uci add_list firewall.passgate_private.dest_ip='172.16.0.0/12'
uci add_list firewall.passgate_private.dest_ip='192.168.0.0/16'
uci set firewall.passgate_private.target='REJECT'

uci set firewall.passgate_paid=rule
uci set firewall.passgate_paid.name='PassGate: paid guests -> internet'
uci set firewall.passgate_paid.src="$GUEST_ZONE"
uci set firewall.passgate_paid.dest="$WAN_ZONE"
uci set firewall.passgate_paid.family='ipv4'
uci set firewall.passgate_paid.proto='all'
uci set firewall.passgate_paid.ipset="$SET"
uci set firewall.passgate_paid.target='ACCEPT'

# Unpaid guests' web requests go to the portal ("Sign in to network")
uci set firewall.passgate_redirect=redirect
uci set firewall.passgate_redirect.name='PassGate: unpaid web -> portal'
uci set firewall.passgate_redirect.src="$GUEST_ZONE"
uci set firewall.passgate_redirect.dest="$PORTAL_ZONE"
uci set firewall.passgate_redirect.family='ipv4'
uci set firewall.passgate_redirect.proto='tcp'
uci set firewall.passgate_redirect.src_dport='80'
uci set firewall.passgate_redirect.dest_ip="$PORTAL_IP"
uci set firewall.passgate_redirect.dest_port='80'
uci set firewall.passgate_redirect.ipset="!$SET"
uci set firewall.passgate_redirect.target='DNAT'

# The guest zone must not forward to the internet on its own, or nobody would need to pay
i=0
while uci -q get "firewall.@forwarding[$i]" >/dev/null; do
	if [ "$(uci -q get "firewall.@forwarding[$i].src")" = "$GUEST_ZONE" ] &&
	   [ "$(uci -q get "firewall.@forwarding[$i].dest")" = "$WAN_ZONE" ]; then
		uci delete "firewall.@forwarding[$i]"
		say "Removed the '$GUEST_ZONE -> $WAN_ZONE' forwarding: guests now get internet only after paying."
		continue
	fi
	i=$((i + 1))
done
# Guests may only reach the router for DNS and DHCP (rules above), not its admin pages
GZ="$(zone_section "$GUEST_ZONE")"
uci set "firewall.$GZ.input=REJECT"
uci set "firewall.$GZ.forward=REJECT"

# Optional local DNS name for the portal
if [ -f /etc/passgate-dns ]; then
	uci -q del_list dhcp.@dnsmasq[0].address="$(cat /etc/passgate-dns)" || true
	rm -f /etc/passgate-dns
fi
if [ -n "$PORTAL_HOST" ]; then
	uci add_list dhcp.@dnsmasq[0].address="/$PORTAL_HOST/$PORTAL_IP"
	printf '%s' "/$PORTAL_HOST/$PORTAL_IP" > /etc/passgate-dns
fi

reload_all

say ""
say "PassGate is set up on this router."
say "In PassGate -> Settings:"
say "  Router: OpenWrt     OpenWrt address: http://<this router's IP as PassGate sees it>"
say "  OpenWrt user: $PG_USER     OpenWrt password: (the PASSWORD you used)     Allow-list name: $SET"
say "then press 'Test Stripe & router connection'."
command -v conntrack >/dev/null 2>&1 || say "Tip: install the 'conntrack' package so ended passes cut open connections right away."
