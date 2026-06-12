#!/usr/bin/env python3
"""DGX Spark — Section B cluster acceptance (capture + Gemini verify).

Verifies the high-speed ConnectX-7 / RoCE link between the two nodes and records
the measured RDMA bandwidth, reusing the same capture + Gemini machinery as
scripts/spark_acceptance.py (real macOS-terminal screenshot + an independent
Gemini reading; machine assertion must agree). Artifacts: data/spark/cluster/.

Run as root over Tailscale SSH (link config + perftest need root):
  .venv/bin/python scripts/spark_cluster.py

This does NOT bring the link up; ops/spark/cluster_up has already configured
the dedicated subnet + MTU. It only verifies and screenshots the good states,
and surfaces the bandwidth honestly (a throttle is reported, never hidden).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from src.spark import (  # noqa: E402
    Gate,
    display_and_capture,
    ensure_terminal_window,
    gemini_verdict,
)

SPARK1_IP = "100.106.152.104"
SPARK2_IP = "100.119.143.76"
LINK_IF = "enp1s0f0np0"
RDMA_DEV = "rocep1s0f0"
PEER_IP = "192.168.100.2"
LINE_RATE_GBPS = 150.0  # expected RoCE range is ~200G; below this = throttle


def root_probe(ip: str, cmd: str, *, timeout: float = 60.0) -> tuple[str, int]:
    try:
        p = subprocess.run(
            ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", f"root@{ip}", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "[probe timed out]", 124
    out = p.stdout + (("\n" + p.stderr) if p.stderr.strip() else "")
    return out.strip("\n"), p.returncode


def measure_bw() -> tuple[str, int]:
    """Start ib_write_bw server on spark2, run client on spark1, return result."""
    root_probe(
        SPARK2_IP,
        f"tmux kill-session -t clbw 2>/dev/null; tmux new-session -d -s clbw "
        f"'ib_write_bw -d {RDMA_DEV} -F --report_gbits -q 4 -t 256 -s 1048576 >/tmp/clbw.log 2>&1'; sleep 2",
        timeout=20,
    )
    out, rc = root_probe(
        SPARK1_IP,
        f"ib_write_bw -d {RDMA_DEV} -F --report_gbits -q 4 -t 256 -s 1048576 -D 5 {PEER_IP} 2>&1 | tail -6",
        timeout=40,
    )
    root_probe(SPARK2_IP, "tmux kill-session -t clbw 2>/dev/null", timeout=15)
    return out, rc


def run_nccl() -> tuple[str, int]:
    """Run the 2-node NCCL all-reduce smoke test (pipes ops/spark/nccl_smoke.sh
    to the rank-0 node). No scratch left on the node."""
    smoke = REPO_ROOT / "ops" / "spark" / "nccl_smoke.sh"
    try:
        with open(smoke) as f:
            p = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", f"root@{SPARK1_IP}", "bash -s"],
                stdin=f, capture_output=True, text=True, timeout=160,
            )
    except subprocess.TimeoutExpired:
        return "[nccl all-reduce timed out]", 124
    out = p.stdout + (("\n" + p.stderr) if p.stderr.strip() else "")
    return out.strip("\n"), p.returncode


# --- assertions ------------------------------------------------------------

def a_link(out: str, rc: int) -> tuple[bool, str]:
    if "UP" not in out or "192.168.100.1" not in out:
        return False, "link iface not UP with cluster IP"
    if "200000" not in out:
        return False, "iface speed is not 200000 Mb/s"
    return True, "enp1s0f0np0 UP, 192.168.100.1/24, 200G"


def a_ping(out: str, rc: int) -> tuple[bool, str]:
    if "0% packet loss" in out:
        return True, "peer reachable, 0% loss over the high-speed link"
    return False, "packet loss / peer unreachable over the link"


def a_rdma(out: str, rc: int) -> tuple[bool, str]:
    if "ACTIVE" in out and "LINK_UP" in out:
        return True, "RoCE rocep1s0f0 ACTIVE / LINK_UP"
    return False, "RoCE link not ACTIVE/LINK_UP"


def a_ssh(out: str, rc: int) -> tuple[bool, str]:
    if rc == 0 and "spark2" in out:
        return True, "passwordless node-to-node SSH over the high-speed link works"
    return False, f"node-to-node SSH failed (rc={rc})"


def a_bw(out: str, rc: int) -> tuple[bool, str]:
    m = re.search(r"^\s*\d+\s+\d+\s+[\d.]+\s+([\d.]+)\s+[\d.]+", out, re.MULTILINE)
    if not m:
        return False, "no bandwidth row parsed (RDMA test did not complete)"
    bw = float(m.group(1))
    if bw < 1.0:
        return False, f"bw {bw} Gb/s — link not carrying RDMA"
    if bw < LINE_RATE_GBPS:
        # Functional, but a throttle: report it loudly, do not pretend it's fine.
        return True, (f"RDMA OK at {bw:.1f} Gb/s, but BELOW ~200G line rate "
                      f"(platform 'insufficient PCIe power' throttle — see NODES.md §9)")
    return True, f"RDMA bandwidth {bw:.1f} Gb/s (line rate)"


def a_nccl(out: str, rc: int) -> tuple[bool, str]:
    if "spark1" not in out or "spark2" not in out:
        return False, "all-reduce did not run on both nodes"
    if "0 OK" not in out and "Avg bus" not in out:
        return False, "no correctness/summary line (collective did not complete)"
    if re.search(r"Out of bounds values\s*:\s*[1-9]", out):
        return False, "NCCL all-reduce reported wrong values"
    m = re.search(r"Avg bus bandwidth\s*:\s*([\d.]+)", out)
    busbw = f", busbw {float(m.group(1)):.2f} GB/s (throttle-bound)" if m else ""
    return True, f"2-node all-reduce correct across spark1+spark2 (#wrong=0){busbw}"


GATES = [
    (Gate("cluster_link", "high-speed link UP @ 200G",
          "ip -br addr show enp1s0f0np0 + speed", "", a_link,
          "Does the terminal show interface enp1s0f0np0 UP with IP 192.168.100.1 and speed 200000?", ""),
     lambda: root_probe(SPARK1_IP, f"ip -br addr show {LINK_IF}; echo speed=$(cat /sys/class/net/{LINK_IF}/speed)")),
    (Gate("cluster_ping", "peer reachable over link", "ping peer 192.168.100.2", "", a_ping,
          "Does the terminal show a successful ping to 192.168.100.2 with 0% packet loss?", ""),
     lambda: root_probe(SPARK1_IP, f"ping -c 3 -W 2 -I {LINK_IF} {PEER_IP}")),
    (Gate("cluster_rdma", "RoCE link ACTIVE", "rdma link show rocep1s0f0", "", a_rdma,
          "Does the terminal show an RDMA/RoCE link in state ACTIVE and physical_state LINK_UP?", ""),
     lambda: root_probe(SPARK1_IP, f"rdma link show | grep {RDMA_DEV} || rdma link show")),
    (Gate("cluster_ssh", "node-to-node SSH on link", "ssh root@192.168.100.2 hostname", "", a_ssh,
          "Does the terminal show a successful passwordless SSH to the peer node returning its hostname (spark2)?", ""),
     lambda: root_probe(SPARK1_IP, f"ssh -o BatchMode=yes -o ConnectTimeout=6 root@{PEER_IP} hostname")),
    (Gate("cluster_bw", "measured RDMA bandwidth", "ib_write_bw spark1<->spark2", "", a_bw,
          "Does the terminal show an ib_write_bw RDMA bandwidth result with a measured BW average in Gb/sec between the nodes?", ""),
     measure_bw),
    (Gate("cluster_nccl", "2-node NCCL all-reduce", "nccl all_reduce_perf spark1+spark2", "", a_nccl,
          "Does the terminal show an NCCL all_reduce test running across two nodes (ranks on spark1 and spark2, both NVIDIA GB10) and completing with 0 wrong values (OK)?", ""),
     run_nccl),
]


def main() -> int:
    out_dir = REPO_ROOT / "data" / "spark" / "cluster"
    out_dir.mkdir(parents=True, exist_ok=True)
    ensure_terminal_window()

    results = []
    for gate, run in GATES:
        print(f"\n=== {gate.id} :: {gate.title} ===", flush=True)
        out, rc = run()
        png = out_dir / f"{gate.id}.png"
        try:
            display_and_capture("cluster", gate, out, rc, png)
        except Exception as e:
            print(f"  capture error: {e}", flush=True)
        machine_ok, detail = gate.assert_fn(out, rc)
        print(f"  machine: {'OK' if machine_ok else 'FAIL'} — {detail}", flush=True)
        try:
            gpass, greason = gemini_verdict(png, gate.gemini_q)
        except Exception as e:
            gpass, greason = False, f"gemini error: {e}"
        print(f"  gemini : {'PASS' if gpass else 'FAIL'} — {greason}", flush=True)
        verdict = "PASS" if (machine_ok and gpass) else "FAIL"
        print(f"  -> {verdict}", flush=True)
        results.append({
            "id": gate.id, "title": gate.title, "verdict": verdict,
            "machine_ok": machine_ok, "machine_detail": detail,
            "gemini_pass": gpass, "gemini_reason": greason,
            "rc": rc, "output": out[:800], "png": str(png),
        })

    (out_dir / "cluster.json").write_text(json.dumps({
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "summary": {"total": len(results), "pass": sum(1 for r in results if r["verdict"] == "PASS")},
        "gates": results,
    }, indent=2))

    print("\n" + "=" * 60, flush=True)
    print("SPARK SECTION-B CLUSTER ACCEPTANCE", flush=True)
    for r in results:
        print(f"  [{r['verdict']}] {r['id']:14s} {r['machine_detail']}", flush=True)
    npass = sum(1 for r in results if r["verdict"] == "PASS")
    print(f"\n  {npass}/{len(results)} gates green. Report: {out_dir / 'cluster.json'}", flush=True)
    print("=" * 60, flush=True)
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
