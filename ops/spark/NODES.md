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

## 8. Future: an Aria "manage the sparks" capability

Goal (later): Aria can set up and health-check the sparks by voice, reusing this
machinery rather than rebuilding it. The smallest natural shape:

- **Factor the gate catalog** out of `scripts/spark_acceptance.py` into a shared
  module (e.g. `src/spark.py`) exposing `probe(node, gate)`, `verify(node, role)
  -> report`, and `setup(node, role)`. The CLI harness and the Aria tools both
  call it — no second implementation.
- **New tools in `src/tools.py`** (per the "new behavior is a new tool, not a new
  layer" rule), wired into the dispatcher:
  - `spark_status(node)` — tier R: run the read-only probes, return JSON.
  - `spark_verify(node, role)` — tier R/X: run the full capture+Gemini gate set,
    post the report + screenshots to the text channel; Aria narrates pass/fail.
  - `spark_setup(node, role)` — tier **X** (executable): runs `setup_node.sh`;
    confirms before firing via the existing risk-tier path.
- **Reuse, don't rebuild:** SSH as the `spark<N>` user (least privilege, not
  root); `screencapture` via the existing `_screenshot_cursor_window` helper;
  the Gemini verdict via the `judge.py` pattern; `ANTHROPIC_API_KEY` stays in
  `.env` and is injected over SSH, never stored in the repo.
- **Failure posture:** identical to preflight — every gate returns
  `(ok, detail, fix)`; a red gate refuses "all clear" and surfaces the fix.

## 9. Sections B and C — defined, deferred (not guessed)

The runbook itself defers these; we do not guess their inputs.

- **Section B — cluster (escape hatch). PHYSICALLY BLOCKED (verified 2026-06-09).**
  Recon of both nodes: the `mlx5_core` / `mlx5_ib` ConnectX-7 driver stack is
  loaded, but **no high-speed netdev exists, no `/sys/class/infiniband` device,
  no RDMA link** — the only wired NIC up is `enP7s7` (Realtek, 1 GbE); the nodes
  talk only over 1 GbE + Tailscale. So the approved 0.5 m 200G QSFP56 DAC cable
  is **not connecting the two CX-7 ports**. This cannot be configured remotely.
  Required physical action: plug the DAC cable port-to-port on the CX-7 ports;
  the mlx5 interface should then enumerate and link. Only then can we do RoCE on
  a dedicated subnet, node-to-node SSH on that link, a **measured** ~200 GbE
  bandwidth check, and an NCCL all-reduce smoke test. Good states otherwise as in
  the runbook checklist. Run only if a model genuinely exceeds 128 GB.
- **Section C — serving (Profile 1, independent workers = the operating mode).**
  Good states: one VLM per node at NVFP4 with KV-cache headroom; both nodes
  pulling from a shared queue; every run manifest records mode + model + version
  + quantization. Blocked on three decisions, each of which changes the build:
  1. **Which VLM** — pick on tagging accuracy via a hand-labeled mini-set bench
     (tagging plan §7), not throughput.
  2. **Queue / object store** — what backs the shared work queue + artifact store.
  3. **NVFP4 weight source** — where the 4-bit weights come from.
  Section C also needs the privileged path noted in §3.
