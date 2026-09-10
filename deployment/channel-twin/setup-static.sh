#!/usr/bin/env bash
set -euo pipefail
export PATH="$PATH:/usr/sbin:/sbin"

usage() {
  echo "Usage: sudo $0 --role ground|satellite --physical-iface IFACE --peer-underlay-ip IPV4 [options]"
  echo "Options: --local-data-ip CIDR --peer-data-ip IPV4 --data-mtu BYTES --scenario FILE"
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
role=""
physical_iface=""
peer_underlay_ip=""
local_data_ip=""
peer_data_ip=""
data_mtu="1450"
scenario_path="$repo_root/configs/channel/scenarios/spaceverse-compatible-static.json"
bridge="br-sgt"
internal_port="sgt-data"
vxlan_port="sgt-vx"
ifb_device="ifb-sgt"
vxlan_id="77"
vxlan_udp_port="4789"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) role="$2"; shift 2 ;;
    --physical-iface) physical_iface="$2"; shift 2 ;;
    --peer-underlay-ip) peer_underlay_ip="$2"; shift 2 ;;
    --local-data-ip) local_data_ip="$2"; shift 2 ;;
    --peer-data-ip) peer_data_ip="$2"; shift 2 ;;
    --data-mtu) data_mtu="$2"; shift 2 ;;
    --scenario) scenario_path="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "This script must run as root." >&2
  exit 2
fi
if [[ "$role" != "ground" && "$role" != "satellite" ]]; then
  echo "--role must be ground or satellite" >&2
  exit 2
fi
if [[ -z "$physical_iface" || -z "$peer_underlay_ip" ]]; then
  usage >&2
  exit 2
fi
if ! ip link show dev "$physical_iface" >/dev/null 2>&1; then
  echo "Physical interface does not exist: $physical_iface" >&2
  exit 2
fi
for command_name in ip tc ovs-vsctl python3; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command is missing: $command_name" >&2
    exit 2
  fi
done
if [[ ! -f "$scenario_path" ]]; then
  echo "Scenario file does not exist: $scenario_path" >&2
  exit 2
fi

# Check the actual route: a VPN or a stale Wi-Fi address can bypass the IFB.
route_json="$(ip -j -4 route get "$peer_underlay_ip")"
local_underlay_ip="$(python3 -c '
import json, sys
route = json.loads(sys.argv[1])[0]
if route.get("dev") != sys.argv[2]:
    sys.exit("Peer route does not use " + sys.argv[2] + ": " + str(route))
print(route["prefsrc"])
' "$route_json" "$physical_iface")"
physical_mtu="$(cat "/sys/class/net/$physical_iface/mtu")"
if [[ ! "$data_mtu" =~ ^[0-9]{3,5}$ ]] || (( 10#$data_mtu < 576 || 10#$data_mtu + 50 > physical_mtu )); then
  echo "Data MTU must be >= 576 and leave 50 bytes for IPv4 VXLAN within physical MTU $physical_mtu" >&2
  exit 2
fi
ovs-vsctl --timeout=10 show >/dev/null

mapfile -t profile_values < <(
  cd "$repo_root"
  python3 -m channel_twin.scenario --scenario "$scenario_path" --receiver-role "$role"
)
if [[ ${#profile_values[@]} -ne 5 ]]; then
  echo "Could not read a complete static profile from $scenario_path" >&2
  exit 2
fi
rate_bps="${profile_values[0]}"
delay_ms="${profile_values[1]}"
jitter_ms="${profile_values[2]}"
loss_pct="${profile_values[3]}"
queue_packets="${profile_values[4]}"

if [[ -z "$local_data_ip" ]]; then
  if [[ "$role" == "ground" ]]; then local_data_ip="10.77.0.1/24"; else local_data_ip="10.77.0.2/24"; fi
fi
if [[ -z "$peer_data_ip" ]]; then
  if [[ "$role" == "ground" ]]; then peer_data_ip="10.77.0.2"; else peer_data_ip="10.77.0.1"; fi
fi

echo "Configuring $role receive path on $physical_iface"
echo "Data endpoint $local_data_ip, peer $peer_data_ip, peer underlay $peer_underlay_ip"
echo "Scenario: $scenario_path"
echo "Inbound profile: rate=$rate_bps bit/s delay=$delay_ms ms jitter=$jitter_ms ms loss=$loss_pct%"

ovs-vsctl --may-exist add-br "$bridge"
ovs-vsctl set-fail-mode "$bridge" standalone
ovs-vsctl --may-exist add-port "$bridge" "$internal_port" -- set Interface "$internal_port" type=internal mtu_request="$data_mtu"
ovs-vsctl --may-exist add-port "$bridge" "$vxlan_port" -- set Interface "$vxlan_port" \
  type=vxlan options:local_ip="$local_underlay_ip" options:remote_ip="$peer_underlay_ip" options:key="$vxlan_id" options:dst_port="$vxlan_udp_port"
ip link set dev "$bridge" up
ip link set dev "$internal_port" up
ip address replace "$local_data_ip" dev "$internal_port"

if ! ip link show dev "$ifb_device" >/dev/null 2>&1; then
  ip link add "$ifb_device" type ifb
fi
ip link set dev "$ifb_device" up

if ! tc qdisc show dev "$physical_iface" | grep -q "qdisc clsact"; then
  tc qdisc add dev "$physical_iface" clsact
fi
if tc filter show dev "$physical_iface" ingress pref 49100 | grep -q .; then
  tc filter delete dev "$physical_iface" ingress pref 49100
fi
tc filter add dev "$physical_iface" ingress pref 49100 protocol ip flower \
  ip_proto udp dst_port "$vxlan_udp_port" src_ip "$peer_underlay_ip" \
  action mirred egress redirect dev "$ifb_device"

tc qdisc replace dev "$ifb_device" root handle 1: htb default 10
tc class replace dev "$ifb_device" parent 1: classid 1:10 htb \
  rate "${rate_bps}bit" ceil "${rate_bps}bit" burst 256k cburst 256k
tc qdisc replace dev "$ifb_device" parent 1:10 handle 10: netem \
  limit "$queue_packets" delay "${delay_ms}ms" "${jitter_ms}ms" loss "${loss_pct}%"

echo "OVS topology:"
ovs-vsctl show
echo "Ingress selector:"
tc -s filter show dev "$physical_iface" ingress pref 49100
echo "Receive-path qdisc:"
tc -s -d qdisc show dev "$ifb_device"
echo "Checking peer data IP (failure is expected until the other endpoint is configured):"
ping -c 1 -W 1 "$peer_data_ip" || true
