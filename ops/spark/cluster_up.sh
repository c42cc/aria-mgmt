#!/usr/bin/env bash
#
# DGX Spark Section B — bring up + persist the high-speed ConnectX-7 link on
# ONE node. Run as root on the node:
#
#   bash cluster_up.sh <CLUSTER_IP_CIDR>
#     spark1:  bash cluster_up.sh 192.168.100.1/24
#     spark2:  bash cluster_up.sh 192.168.100.2/24
#
# Idempotent. Configures the CX-7 port with a dedicated subnet (separate from
# the LAN/Tailscale path) + jumbo MTU and persists it via netplan so it survives
# reboot. Node-to-node SSH setup and RoCE/bandwidth verification are driven from
# the Mac by scripts/spark_cluster.py. See ops/spark/NODES.md §9.
#
# NOTE: the link comes up at 200G (RoCE ACTIVE) but is currently throttled to
# ~12.8 Gb/s by a platform "insufficient PCIe slot power" condition on the
# Spark's CX-7 — a line-rate fix needs NVIDIA's DGX Spark clustering bring-up,
# not OS config. This script does the OS-side bring-up only.

set -euo pipefail

IFACE="${SPARK_LINK_IFACE:-enp1s0f0np0}"
CIDR="${1:?usage: cluster_up.sh <ip/cidr, e.g. 192.168.100.1/24>}"

command -v ip >/dev/null 2>&1 || { echo "FATAL: iproute2 (ip) missing" >&2; exit 1; }
[ -e "/sys/class/net/$IFACE" ] || { echo "FATAL: $IFACE not present (is the QSFP cable connected?)" >&2; exit 1; }

ip link set "$IFACE" up
ip addr replace "$CIDR" dev "$IFACE"
ip link set "$IFACE" mtu 9000

cat > /etc/netplan/99-spark-cluster.yaml <<EOF
network:
  version: 2
  ethernets:
    $IFACE:
      addresses: [$CIDR]
      mtu: 9000
EOF
chmod 600 /etc/netplan/99-spark-cluster.yaml
netplan apply

echo "cluster link up + persisted: $IFACE $CIDR mtu 9000"
ip -br addr show "$IFACE"
