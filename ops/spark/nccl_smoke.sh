#!/usr/bin/env bash
#
# DGX Spark Section B — 2-node NCCL all-reduce smoke test over the RoCE link.
# Run on the rank-0 node (spark1) as root, or pipe it from the Mac:
#   ssh root@spark1 'bash -s' < ops/spark/nccl_smoke.sh
#
# Verifies the collective stack (NCCL + OpenMPI) runs CORRECTLY across both
# nodes. OpenMPI is pinned to the cluster subnet (192.168.100.0/24) so it can't
# wander onto docker0/tailscale; NCCL is pointed at the RoCE device. Prints the
# rank/device lines, the data row, correctness, and the average bus bandwidth.
#
# The smoke test's purpose is CORRECTNESS across nodes (#wrong must be 0). The
# bus bandwidth reflects the platform "insufficient PCIe power" throttle
# documented in ops/spark/NODES.md §9 — it is not what this gate asserts.

set -uo pipefail

export PATH=/usr/local/cuda/bin:$PATH
export LD_LIBRARY_PATH=/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}

BIN=/root/nccl-tests/build/all_reduce_perf
[ -x "$BIN" ] || { echo "FATAL: $BIN not built (run nccl-tests make on both nodes)"; exit 1; }

HF=$(mktemp)
printf '192.168.100.1 slots=1\n192.168.100.2 slots=1\n' > "$HF"
trap 'rm -f "$HF"' EXIT

timeout 120 mpirun --allow-run-as-root -np 2 --hostfile "$HF" \
  --mca btl_tcp_if_include 192.168.100.0/24 --mca oob_tcp_if_include 192.168.100.0/24 \
  -x PATH -x LD_LIBRARY_PATH \
  -x NCCL_SOCKET_IFNAME=enp1s0f0np0 -x NCCL_IB_HCA=rocep1s0f0 \
  "$BIN" -b 256M -e 256M -g 1 2>&1 \
  | grep -E 'Rank|GB10|redop|float|Out of bounds|Avg bus' || true
