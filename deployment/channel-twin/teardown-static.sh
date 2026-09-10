#!/usr/bin/env bash
set -euo pipefail
export PATH="$PATH:/usr/sbin:/sbin"

physical_iface=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --physical-iface) physical_iface="$2"; shift 2 ;;
    --help|-h) echo "Usage: sudo $0 --physical-iface IFACE"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [[ $EUID -ne 0 || -z "$physical_iface" ]]; then
  echo "Usage: sudo $0 --physical-iface IFACE" >&2
  exit 2
fi
if ! ip link show dev "$physical_iface" >/dev/null 2>&1; then
  echo "Physical interface does not exist: $physical_iface" >&2
  exit 2
fi

if tc filter show dev "$physical_iface" ingress pref 49100 2>/dev/null | grep -q .; then
  tc filter delete dev "$physical_iface" ingress pref 49100
fi
if ip link show dev ifb-sgt >/dev/null 2>&1; then
  tc qdisc delete dev ifb-sgt root 2>/dev/null || true
  ip link delete ifb-sgt
fi
if ovs-vsctl br-exists br-sgt; then
  ovs-vsctl del-br br-sgt
fi
echo "Removed channel-twin filter pref 49100, IFB ifb-sgt, and OVS bridge br-sgt."
echo "The shared clsact qdisc on $physical_iface was preserved to avoid deleting unrelated filters."
