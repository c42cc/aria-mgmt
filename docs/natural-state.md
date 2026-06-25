# The Natural State — unified dev-environment management on the Sparks

This is the operating map for the substrate landed on branch `feat/natural-state-spark`.
Two planes on the Sparks, one mesh (Tailscale), frozen contracts, **reuse over
rebuild**. Aria manages the development environment; you oversee via Zed + the
single pane; Home Assistant is the management surface; the Floor (NAS) is loudly
absent until it arrives.

## Planes (logical role -> box)

| Plane | Box | What runs |
|---|---|---|
| **Mind** | spark1 `100.106.152.104` | vLLM serving `local-brain` (gpt-oss-120b) behind the Anthropic Messages API on `:8000`; Home Assistant container on `:8123` |
| **Hands** | spark2 `100.119.143.76` | build cells = Claude Code on the node, on isolated branches, $0 (node key) |
| **Floor** | NAS (arrives tomorrow) | state of record — ABSENT/loud until mounted at `FLOOR_ROOT` |
| **Cloud** | Opus 4.8 | judgment (conductor), via `ANTHROPIC_API_KEY`, capped by `DAILY_SPEND_CAP_USD` |
| **You** | Mac / phone | Zed direct-drive over the mesh; one-tap approvals (the go-gate) |

## The single pane

`make doctor` — every plane's live health (Mind/Hands/Floor/HA/Cloud), today's
spend, and the last request. Each probe is loud and names the one fix; a down or
absent plane says so plainly (never green-by-omission); a connectivity failure is
ours to surface, never blamed on the node/hub/network. `--json` for machines.

## The primitives (what was built / reused)

- **Inference contract** — `SPARK_BASE_URL` -> vLLM `local-brain`; consumed by
  `src/dispatcher.py::_run_spark` + `loops/local-ask.yaml`. Local, private, $0 cloud.
- **The Floor** — `src/floor.py`: the storage-layout contract as one honest module.
  ABSENT until a real NAS **mountpoint** is at `FLOOR_ROOT` (a local dir is refused —
  never let compute disk masquerade as the Floor). `require()` halts; no fallback.
- **The cell on the Hands** — `src/spark.py::run_audit` (+ `scripts/spark_cc.py`):
  the one Claude Code engine relocated onto the node. Billing mirrors the Mac engine
  (`subscription` | `api`). Each cell branches from a **clean main base** (isolation).
- **Aria the supervisor** — `hands` dispatcher endpoint + `loops/hands-build.yaml`:
  plan -> dispatch a cell -> oversee -> verify GROUND TRUTH (the node's git shows a
  real commit) -> report. Never the cell's narration; loud on launch/auth/timeout.
- **Visual verification** — `src/visual_verify.py`: headless-Chrome capture + the ONE
  Gemini judge reused from `src/home_verify.py::gemini_verdict` (browser render + HA
  camera = two capture sources, one judge). Deterministic checks gate; Gemini is the
  recorded screenshot-correctness observer.
- **The management surface** — Home Assistant container on the Mind; the DEV
  ENVIRONMENT is the managed domain (`input_boolean.aria_dev_environment`,
  `…mind_inference`). `src/homeassistant.py` actuates + verifies by ground-truth
  state re-read. `scripts/ha_onboard.py` does headless onboarding + token minting.
- **Zed oversight** — `make zed-hands` opens spark2 in Zed over Tailscale SSH; the
  workspace surfaces live cell runs at `.cells/` and the cell branches.

## How to operate

```
make doctor                              # the single pane
make spark-serve SPARK_NODE=spark1       # bring the Mind up (idempotent)
make spark-serve-stop SPARK_NODE=spark1  # tear the Mind down (weights kept)
make zed-hands                           # oversee/steer the Hands in Zed
python -m src.visual_verify --url <u> --question "<intent>"   # capture + Gemini verdict
python scripts/ha_onboard.py             # (re)mint HASS_TOKEN on a fresh hub
```

## Self-management, lockdown & data sovereignty (the "runs itself" survey)

It is built to run itself and be relied on, with the fewest moving parts:

- **Self-recovery.** The Mind (vLLM) and Home Assistant containers run
  `--restart unless-stopped`, and docker is enabled on boot — so they auto-recover
  from a crash AND survive a reboot. This is NOT a silent heal: the doctor surfaces
  a down/flapping plane, so recovery is observable. A deliberate `stop` stays stopped.
  *Proven*: killing HA's process auto-restarted it (RestartCount 0->1, back up in ~8s).
- **Reliability over cost-shaving.** The conductor (the part that decides + drives the
  go-gate) is **Opus on every turn** by default. A live test caught the fast tier
  (haiku) skipping the CONFIRM phase, which silently jammed the go-gate; all-Opus
  sequences CONFIRM->DISPATCH correctly. You're paying for reliability where it counts.
- **Best judge.** The one Gemini visual judge prefers **gemini-3.1-pro-preview**
  (verified live), with 2.5-flash as the fast fallback.
- **Data sovereignty.** Local stays local: the local model (gpt-oss-120b), Home
  Assistant, the conversation store, and the Floor never leave the tailnet. The ONLY
  egress is the cloud judgment path (Opus) and the visual judge (Gemini):
  - **Anthropic (Opus)** runs on commercial/API terms — no training on your prompts
    (keep the account OUT of the Development Partner Program).
  - **Gemini (visual judge)** sends only screenshots/frames. Use a **paid** Google key
    (paid tier is not trained on; the free tier is). For sensitive HOME camera frames
    later, prefer a LOCAL vision model on the Mind so images never leave — recorded as
    the next data-sovereignty step.
- **Exposure.** `:8000` (Mind) and `:8123` (HA) bind the LAN + tailnet, behind the home
  router/NAT — no public route. HA is token-protected; the Mind answers only on your
  network. Tighten the Mind to tailnet-only by binding its port to the tailnet IP if
  you ever want to (one line in `serve_model.sh`); not needed for a trusted home.
- **Secrets** live only in `.env` (gitignored; nothing secret is tracked in git).
- **Simplicity.** Self-management = two primitives: auto-restart (recover) + `make doctor`
  (observe). No extra daemon. If you want it to *ping you* when a plane is down, that's a
  small scheduled doctor-check — a deliberate add, not built by default.

## Verified (the proofs, watched go green)

- Mind: `/v1/models` -> `local-brain`; a real `local-ask` delivered at **$0 cloud** in ~3s.
- Hands: a cell built a file == the exact nonce (ground truth); two cells **merge clean**
  (octopus RC=0); the supervisor dispatch delivered + verified end-to-end.
- Visual: a cell built `ns_viz.html`; headless render -> Gemini **PASS** (green circle),
  **FAIL** a wrong criterion (red square) so wrong claims are blocked, **stable** x3.
- Home: `actuate()` flips dev-env entities and ground-truth confirms on/off (vs Aria, not the Shield).
- Fail-safe: bad HA token -> loud 401 fix; dead Mind -> loud "is vLLM serving?"; **no fallbacks**.

## Seams + decisions on record (so they don't drift)

- **One Gemini judge** spans `home_verify` (HA camera) + `visual_verify` (browser). The
  LV4 `lib/gemini_judge.py` is a separate, richer judge in that repo — collapsing all three
  into one is a scheduled follow-up, recorded here so it isn't silent drift.
- **HA runs in docker, not rootless podman** — docker works on spark1 without sudo, so it
  meets the goal (a container on the Mind) with the fewest moving parts. Podman would have
  needed a root install.
- **Mind model = gpt-oss-120b** at `gpu_mem_util=0.85`, `max_model_len=65536`. The
  `qwen3-30b-a3b` registry entry was fixed to its real ceiling (`max_model_len=40960`).
- **The ConnectX/NCCL cluster** (`ops/spark/cluster_up.sh`) is present but **dormant**.
- **A standing GPU process was stopped to free the Mind**: `hologram_service.worker.serve_quality`
  (port 3010, `~/holo` on spark1). Restart:
  `ssh spark1 'cd ~/holo && setsid bash -c "export PATH=\$HOME/.local/bin:\$PATH; export CUDA_HOME=/usr/local/cuda-13.0; export ATTN_BACKEND=xformers; export SPARSE_ATTN_BACKEND=xformers; cd ~/holo; exec .venv/bin/python -m hologram_service.worker.serve_quality 3010 >worker.log 2>&1" </dev/null &'`

## Deferred (anti-scope)

The full saturation-ceiling hunt; the real Floor (NAS redundancy/backup, tomorrow ->
flip `FLOOR_ROOT`); relocating LV4 capture onto spark2/Linux; the full "her" media/voice
fan-out; collapsing the three Gemini-judge homes into one.
