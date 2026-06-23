# DGX Spark nodes — Section A execution notes

Working notes from making the [DGX Spark engineer runbook](../../) Section A
manifest on the two GB10 nodes, plus the verification contract and the design
for a future Aria "manage the sparks" capability. Source of truth for *how we
actually reach and verify the sparks*. Keep it current.

Artifacts of each acceptance run live under `data/spark/<node>/` (one PNG per
gate + `acceptance.json`).

---

## 1. The two nodes (facts, verified)

| node | tailnet IP | user | OS / arch | GPU | unified mem |
|---|---|---|---|---|---|
| spark1 | `100.106.152.104` | `spark1` (uid 1000, in `sudo`) | Ubuntu 24.04.4, aarch64, kernel 6.17 nvidia | NVIDIA GB10 (driver 580.159.03, CUDA 13.0) | 121 GiB |
| spark2 | `100.119.143.76` | `spark2` (uid 1000) | Ubuntu 24.04, aarch64 | NVIDIA GB10 (driver 580) | 121 GiB |

Both are on tailnet `tail216537.ts.net`, owned by `corbin.c.chase@`. They are
**not** tagged `tag:spark` (the runbook's assumption); ACLs key off the owner.

SSH aliases were added to `~/.ssh/config` (`spark1`, `spark2` → the tailnet IPs).

## 2. How we reach them (access reality)

This is the dysfunctional primitive the runbook glosses over: **access is not
uniform across the two nodes.**

- **spark1 — Tailscale SSH is enabled.** Connecting to `spark1:22` is intercepted
  by `tailscaled` and authenticated by tailnet identity (no SSH key needed). We
  get in as `spark1@…` (the human user) or `root@…`. `sudo` for the `spark1`
  user **requires a password we do not have**.
- **spark2 — Tailscale SSH NOW enabled (was stock OpenSSH).** Originally
  `spark2:22` was stock sshd and denied every key path `(publickey,password)`.
  Installing this Mac's key into `~/.ssh/authorized_keys` with correct perms
  (0600 key, 0700 `.ssh`, 0750 home) did **not** help — sshd still rejected it,
  so its `AuthorizedKeysFile`/accepted-algorithms are locked down (sshd_config
  is not world-readable). The clean fix made it symmetric with spark1: with the
  login password, `sudo tailscale set --ssh` once. spark2 now authenticates by
  tailnet identity (`ssh spark2`, `root@<ip>`) with no key/password. See §7.

MagicDNS does **not** resolve from the Mac shell, and the macOS `tailscale ssh`
wrapper defers to the system resolver, so it fails. Use the **tailnet IPs**.

### Two macOS shell gotchas (both real, both cost us time)
- The interactive shell has `err_exit` set: a failing `ssh` aborts a multi-line
  script. Append `|| true` (or run the body in `bash`) when probing.
- `ssh` without `-n` swallows the rest of the script from stdin (eats loop
  bodies). Always probe with `ssh -n`.

## 3. The decision that matters: user-level, no root

Because sudo/root is **not uniformly available** (no spark1 sudo password; no
spark2 root at all), we do **not** follow the runbook's apt/nodesource path.
Instead everything installs **at user level into `~/.local/bin`**, as the
node's normal user. One PATH entry (`~/.local/bin`, exported by
`~/.config/spark/env.sh`) makes the whole toolchain visible to non-interactive
SSH probes. This is the uniform, fewest-moving-parts path that works on every
node regardless of privilege, and it fully satisfies Section A's good states.

Deviations from the runbook, and why (all intentional, none silent):
- **Node** via the official ARM64 tarball, not `nodesource`/apt (no root). Pinned
  to the latest v22 LTS, resolved at install time via `nodejs.org/dist/index.json`.
- **ripgrep**: not apt-installed; Claude Code's **bundled** binary is used
  (`USE_BUILTIN_RIPGREP=1`, not the runbook's `0`).
- **build-essential / ca-certificates**: skipped — not needed for the Section A
  gate (no compilation; HTTPS already works).
- **CLAUDE.md** placed at `~/.claude/CLAUDE.md` (user-global memory, always
  loaded) rather than a repo root, since no pipeline repo exists yet.
- **filesystem MCP** rooted at `$HOME` (no pipeline repo yet).
- **Auth** is headless via `ANTHROPIC_API_KEY` (from this repo's `.env`), written
  to `~/.config/spark/anthropic_key` (0600), never echoed or screenshotted.

When Section C (serving) lands it **will** need a privileged path (vLLM, system
deps). Arrange sudo/root then; it is deliberately out of scope for Section A.

## 4. Run it

```bash
# Provision a node (idempotent). Key is read from .env; never printed.
KEY=$(grep '^ANTHROPIC_API_KEY=' .env | cut -d= -f2-)
ssh -o BatchMode=yes spark1 "ANTHROPIC_API_KEY='$KEY' bash -s -- A spark1" < ops/spark/setup_node.sh

# Verify a node (capture + Gemini visual confirm). --run-setup does both.
.venv/bin/python scripts/spark_acceptance.py --node spark1 --role A
.venv/bin/python scripts/spark_acceptance.py --node spark1 --role A --run-setup
.venv/bin/python scripts/spark_acceptance.py --node spark1 --role A --only gpu,settings
```

Roles: spark1 = `A` (worker A), spark2 = `B` (worker B / rank-0 cluster head).

## 5. The good-state contract (what "good" means, and how it's proven)

`scripts/spark_acceptance.py` checks nine gates. Each gate is proven **twice**
and both must agree, or it FAILS loudly (a machine/Gemini disagreement is itself
a failure — never silently resolved):

1. a **machine assertion** over the probe's stdout + exit code (ground truth via
   `ssh -n`), and
2. an **independent Gemini visual verdict** over a screenshot of a real macOS
   Terminal showing that probe's output (`screencapture -R`; the same
   `screencapture` primitive `src/tools.py::_screenshot_cursor_window` uses; the
   Gemini call mirrors `src/judge.py`, temperature 0).

| gate | good state | machine assertion |
|---|---|---|
| `claude_version` | Claude Code ≥ 2.1.100 | semver ≥ 2.1.100, rc 0 |
| `claude_doctor` | install/config healthy | no failure markers, rc 0 |
| `claude_auth` | headless model round-trip | `claude -p "…OK"` returns OK, rc 0 |
| `node_version` | Node ≥ 18 | major ≥ 18 |
| `gpu` | GB10 + ~128 GB | `GB10` present, no smi error, Mem ≥ 100 GiB |
| `toolchain` | git/tmux/jq/curl/node/npm/uv/claude resolve | none MISSING |
| `settings` | stable channel + floor + ripgrep | `stable` + `minimumVersion` + `USE_BUILTIN_RIPGREP` |
| `claude_md` | node identity present | names node + worker role + GB10 |
| `mcp` | filesystem MCP registered | `filesystem` in `claude mcp list` |

`claude_auth` is the closest-to-end-user proof: it authenticates the key and
round-trips a real model answer, not just a version string.

Resilience (our responsibility, never the service's fault): the Gemini call
retries with jittered backoff and falls back across
`gemini-2.5-flash → gemini-2.0-flash → gemini-flash-latest`, so a model-demand
spike cannot fail a genuinely-good gate.

## 6. Results

- **spark1 (worker A): 9/9 GREEN.** Claude Code 2.1.153 (stable), Node v22.22.3,
  uv 0.11.19, settings + identity in place, filesystem MCP `✓ Connected`,
  headless auth round-trip returns OK, GB10 + 121 GiB confirmed. Artifacts:
  `data/spark/spark1/` (9 PNGs + `acceptance.json`).
- **spark2 (worker B): 9/9 GREEN.** Same toolchain (Claude Code 2.1.153, Node
  v22.22.3, uv 0.11.19), identity = worker node B, filesystem MCP `✓ Connected`,
  GB10 + 121 GiB. Artifacts: `data/spark/spark2/`. Reached via Tailscale SSH
  (enabled per §7).

## 7. spark2 access — RESOLVED via Tailscale SSH

spark2 originally had no Tailscale SSH and rejected our key even when correctly
installed (its sshd `AuthorizedKeysFile`/algorithms are locked down). The fix
that worked, done once with the login password:

```bash
sudo tailscale set --ssh    # run on spark2; idempotent, non-disruptive
```

After this, `ssh spark2` and `ssh root@100.119.143.76` authenticate by tailnet
identity with no key or password — symmetric with spark1. (`tailscale set` is
preferred over `tailscale up --ssh`, which resets other prefs.) For future
nodes, enabling Tailscale SSH up front is the least-friction path.

## 8. Aria "manage the sparks" capability — BUILT

Aria can health-check, prove, and (re)provision the sparks by voice or `#ucs`
text, reusing this machinery rather than rebuilding it. Shipped as designed:

- **Shared module `src/spark.py`** holds the node registry, the SSH ground-truth
  probe, the gate catalog (machine assertion + Gemini question + fix), the macOS
  Terminal capture, the Gemini verdict, and three high-level ops: `status(node)`,
  `verify(node, role)`, `setup(node, role)`. The CLI harness
  (`scripts/spark_acceptance.py`) and `scripts/spark_cluster.py` import from it —
  **one implementation, not two.**
- **Three tools in `src/tools.py`**, dispatched by `handle_tool_call` and exposed
  in both Aria's voice catalog (`src/gemini_session.py::TOOL_DECLARATIONS`) and
  the `do_with_claude` text-agent catalog (`_LOCAL_TOOL_*`):
  - `spark_status(node)` — **read-only, free.** Identity / GPU + unified memory /
    toolchain + versions / cluster-link state, for one node or both. No Terminal,
    no Gemini, no model spend. The fast "how are the sparks?".
  - `spark_verify(node, role)` — the full capture+Gemini gate set; posts a
    per-gate report to the text channel and saves PNGs under `data/spark/<node>/`.
  - `spark_setup(node, role)` — **executable**: runs `setup_node.sh`. Per-command
    confirmation is OFF system-wide (`CONFIRM_RISKY_TOOLS=false`); approval lives
    at the approach level, so Aria offers it via `propose_action` (one tap), the
    same posture as `create_42c_account`.
- **Reuse, not rebuild:** SSH as the `spark<N>` user (least privilege, not root);
  the Gemini verdict via the `judge.py` pattern; `ANTHROPIC_API_KEY` stays in
  `.env` and is injected over SSH, never stored in the repo or printed.
- **Boot health:** a non-blocking advisory `sparks` preflight probe (WARN) checks
  reachability of both nodes in parallel — a powered-off spark is surfaced in
  `#ucs-alerts`, never a ready-state blocker.
- **Failure posture:** identical to preflight — every gate returns `(ok, detail)`
  with the runbook fix; a red gate refuses "all clear" and a machine/Gemini
  disagreement is itself a loud FAIL.

## 9. Sections B and C — defined, deferred (not guessed)

The runbook itself defers these; we do not guess their inputs.

- **Section B — cluster (escape hatch). BROUGHT UP + VERIFIED, but BANDWIDTH
  THROTTLED (2026-06-09).** With the QSFP56 DAC cable connected, both CX-7 cards
  enumerate: `enp1s0f0np0` and `enP2p1s0f0np0` come up at **200 Gb/s with RoCE
  ACTIVE** on each node. Brought up via `ops/spark/cluster_up.sh`: a dedicated
  subnet `192.168.100.0/24` (+ jumbo MTU 9000) on `enp1s0f0np0`, persisted via
  netplan.   Verified by `scripts/spark_cluster.py` (capture + Gemini), **6/6**:
  link UP @200G, peer ping 0% loss, RoCE `rocep1s0f0` ACTIVE/LINK_UP,
  passwordless **node-to-node SSH** over the link (root key, both directions),
  measured RDMA bandwidth, and the **2-node NCCL all-reduce** (below).
  Artifacts: `data/spark/cluster/`.
  - **BANDWIDTH FINDING (the gate caught it):** measured RDMA throughput is only
    **~12.8 Gb/s** (both `ib_send_bw` and `ib_write_bw`, both CX-7 links, across
    QP / MTU / message-size tuning) — a fraction of the 200G line rate. This is
    **not** an OS/RoCE-config issue: no SW rate limit (PFC off, `tc` unlimited,
    NIC advertises up to 195 Gbps), PCIe is Gen5 x4 (~126 Gb/s capable), Ethernet
    negotiated 200G, CPU governor `performance`. The cause is in dmesg on both
    cards: `mlx5_core … Detected insufficient power on the PCIe slot (27W)` — a
    DGX Spark platform/power condition. A line-rate fix needs NVIDIA's DGX Spark
    clustering bring-up (power/firmware) per the runbook's "use NVIDIA's official
    playbooks" note — not an OS patch. Until fixed, distributed inference
    (Profiles 2/3) is bandwidth-bound; the recommended **independent-worker**
    mode (Profile 1) does not use this link and is unaffected.
  - **NCCL all-reduce smoke test: PASSES.** NCCL 2.30.7 + OpenMPI 4.1.6 are
    installed on both nodes and nccl-tests is built for sm_121 (under
    `/root/nccl-tests`). `ops/spark/nccl_smoke.sh` runs a 2-node all-reduce
    (ranks on spark1 + spark2, both GB10) — **correct: `#wrong=0`, "Out of bounds
    values : 0 OK"** — pinning OpenMPI to the `192.168.100.0/24` subnet and NCCL
    to the RoCE device. Verified by the `cluster_nccl` gate (capture + Gemini).
    busbw is ~1.2 GB/s, bound by the same throttle above: the smoke test proves
    the collective stack is **correct**, not that it is fast.
- **Section C — serving (Profile 1, independent workers = the operating mode).**
  **The CHAT-AGENT serving path is BUILT (2026-06-23).** A single open-source LLM
  is served on one node behind the Anthropic Messages API (`vllm serve` →
  `/v1/messages`, which vLLM serves natively), and Aria's unchanged agent loop
  runs on it by pointing `ANTHROPIC_BASE_URL` at the node — the "Local Spark
  Agent" (browser chat window, `src/local_chat_web.py`). Stand it up with
  `ops/spark/serve_model.sh` (Mac side: `src/spark.py::serve_start` /
  `make spark-serve`); verify the good states (server up, chat, the `tool_use`
  round-trip, cache_control tolerance, GPU residency) twice with
  `scripts/spark_serve.py` (capture + Gemini), bench the model with `--bench`, and
  certify a real tool-backed request with `scripts/live_meter.py --base-url`. The
  default candidates are gpt-oss-120b (MXFP4, most capable but quick) and a
  30B-A3B (snappier); the bench picks the default. This needs the privileged path
  noted in §3 (docker is present on the nodes; the user-level uv-venv engine is
  the least-privilege alternative).

  The original **VLM tagging-pipeline** flavor of Section C (batch image tagging,
  shared queue) is a DIFFERENT workload and remains deferred on its own three
  decisions, each of which changes that build:
  1. **Which VLM** — pick on tagging accuracy via a hand-labeled mini-set bench
     (tagging plan §7), not throughput.
  2. **Queue / object store** — what backs the shared work queue + artifact store.
  3. **NVFP4 weight source** — where the 4-bit weights come from.
  Section C also needs the privileged path noted in §3.
