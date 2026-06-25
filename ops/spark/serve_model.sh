#!/usr/bin/env bash
#
# DGX Spark — serve an open-source model behind the Anthropic Messages API.
#
# Runs ON a spark node. Stands up vLLM and launches `vllm serve` so that
# /v1/messages (Anthropic-compatible, used unchanged by Aria's agent loop) and
# /v1/models (health) are reachable on 0.0.0.0:$PORT over the Tailscale LAN.
#
# This is the dysfunctional-primitive collapse: the agent's brain stops being a
# remote metered cloud call and becomes a local weights artifact behind the SAME
# Messages API the loop already speaks. The Mac side only flips ANTHROPIC_BASE_URL.
#
# Subcommands:
#   start    bring the server up (idempotent: a healthy server is a no-op)
#   stop     tear the server down (tmux session / docker container); weights cache kept
#   status   tmux/container liveness + /v1/models + GPU residency (machine-readable-ish)
#   health   exit 0 iff /v1/models answers with the served model
#   logs     tail the serve log
#
# Engine (SERVE_ENGINE): auto (default) | venv | container
#   venv      user-level uv venv at ~/.local/vllm-venv (NODES.md §3 least-privilege)
#   container NGC image (needs docker + nvidia-container-toolkit; root)
#   auto      venv if vLLM imports there, else container if docker is present
#
# Idempotent + loud (set -uo pipefail; explicit FATALs; no silent fallbacks).
# Survives SSH drops: venv runs under tmux, container under docker -d.
# Halt-don't-heal: --restart no on the container, so a crash stays down and is
# surfaced by the serve gate — never silently restarted into a wrong state.
#
# Invoke from the Mac (src/spark.py::serve_* pipe this script):
#   ssh sparkN "MODEL=... SERVED_NAME=... TOOL_PARSER=... bash -s -- start" < ops/spark/serve_model.sh

set -uo pipefail

CMD="${1:-status}"

# --- configuration (env overridable; src/spark.py sets these) ---------------
MODEL="${MODEL:-openai/gpt-oss-120b}"
SERVED_NAME="${SERVED_NAME:-local-brain}"
TOOL_PARSER="${TOOL_PARSER:-openai}"
PORT="${PORT:-8000}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-65536}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.80}"
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8}"
SERVE_ENGINE="${SERVE_ENGINE:-auto}"
VLLM_IMAGE="${VLLM_IMAGE:-nvcr.io/nvidia/vllm:26.04-py3}"
EXTRA_ARGS="${EXTRA_ARGS:-}"

SESSION="vllm_serve"                       # tmux session AND docker container name
VENV="$HOME/.local/vllm-venv"
RUNDIR="$HOME/.cache/spark_serve"
LOG="$RUNDIR/serve.log"
META="$RUNDIR/meta.txt"
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"

mkdir -p "$RUNDIR" "$HF_CACHE"

log() { printf '[serve %s] %s\n' "$(hostname -s 2>/dev/null || echo node)" "$*"; }
die() { printf '[serve] FATAL: %s\n' "$*" >&2; exit 1; }

# Put the user-level toolchain (uv, etc.) on PATH for non-interactive SSH.
[ -f "$HOME/.config/spark/env.sh" ] && . "$HOME/.config/spark/env.sh"

# --- engine resolution ------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

venv_has_vllm() {
  [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import vllm" >/dev/null 2>&1
}

resolve_engine() {
  case "$SERVE_ENGINE" in
    venv|container) echo "$SERVE_ENGINE"; return 0 ;;
    auto)
      if venv_has_vllm; then echo venv; return 0; fi
      if have docker; then echo container; return 0; fi
      # Last try: we can still build the venv on demand in start_venv.
      echo venv; return 0
      ;;
    *) die "unknown SERVE_ENGINE '$SERVE_ENGINE' (auto|venv|container)" ;;
  esac
}

# The vLLM serving flags are identical across engines. /v1/messages is hoisted
# into `vllm serve`'s OpenAI server (vLLM #27882), so this one entrypoint gives
# the agent loop the Anthropic Messages API it needs.
vllm_args() {
  printf '%s' "serve $MODEL \
--served-model-name $SERVED_NAME \
--host 0.0.0.0 --port $PORT \
--enable-auto-tool-choice --tool-call-parser $TOOL_PARSER \
--enable-prefix-caching \
--kv-cache-dtype $KV_CACHE_DTYPE \
--gpu-memory-utilization $GPU_MEM_UTIL \
--max-model-len $MAX_MODEL_LEN \
--trust-remote-code $EXTRA_ARGS"
}

write_meta() {
  {
    echo "started_at=$(date -Is)"
    echo "engine=$1"
    echo "model=$MODEL"
    echo "served_name=$SERVED_NAME"
    echo "tool_parser=$TOOL_PARSER"
    echo "port=$PORT"
    echo "max_model_len=$MAX_MODEL_LEN"
  } > "$META"
}

# --- health -----------------------------------------------------------------
health() {
  local body
  body="$(curl -fsS --max-time 5 "http://127.0.0.1:$PORT/v1/models" 2>/dev/null)" || return 1
  printf '%s' "$body" | grep -q "$SERVED_NAME" || return 1
  return 0
}

# --- venv engine ------------------------------------------------------------
ensure_venv() {
  if venv_has_vllm; then return 0; fi
  have uv || die "uv not found (run ops/spark/setup_node.sh first); cannot build the vLLM venv. \
Set SERVE_ENGINE=container to use the NGC image instead."
  log "creating vLLM venv at $VENV (uv) — this downloads vLLM + CUDA wheels, can take minutes"
  uv venv "$VENV" --python 3.12 >/dev/null 2>&1 || uv venv "$VENV" >/dev/null 2>&1 || die "uv venv failed"
  # SM121 (GB10) support landed in recent vLLM; install the latest unless pinned.
  uv pip install --python "$VENV/bin/python" "vllm${VLLM_PIN:+==$VLLM_PIN}" \
    || die "uv pip install vllm failed (try SERVE_ENGINE=container; SM121 wheels can lag)"
  venv_has_vllm || die "vLLM still not importable in $VENV after install"
}

start_venv() {
  ensure_venv
  local args; args="$(vllm_args)"
  log "launching (venv) under tmux session '$SESSION': vllm $args"
  tmux kill-session -t "$SESSION" 2>/dev/null || true
  tmux new-session -d -s "$SESSION" \
    "source '$VENV/bin/activate'; exec vllm $args >> '$LOG' 2>&1"
  write_meta venv
}

# --- container engine -------------------------------------------------------
start_container() {
  have docker || die "docker not found and SERVE_ENGINE=container requested"
  local args; args="$(vllm_args)"
  log "launching (container) '$SESSION' from $VLLM_IMAGE: vllm $args"
  docker rm -f "$SESSION" >/dev/null 2>&1 || true
  # --restart unless-stopped: this is the home nervous system — it must self-recover
  # from a crash AND survive a reboot (docker is enabled on boot). NOT silent: the
  # doctor (src/doctor.py) surfaces a down/flapping Mind, so recovery is observable,
  # never a hidden heal. A deliberate `serve_model.sh stop` still stays stopped.
  # Override with RESTART_POLICY=no for a one-off build-verifier serve.
  # MXFP4 MoE (gpt-oss) wants the FlashInfer FP4 path; harmless for other models.
  docker run -d --name "$SESSION" --restart "${RESTART_POLICY:-unless-stopped}" \
    --gpus all --shm-size=16g \
    -v "$HF_CACHE:/hf-cache" -e "HF_HUB_CACHE=/hf-cache" \
    -e VLLM_USE_FLASHINFER_MOE_FP4=1 -e VLLM_FLASHINFER_MOE_BACKEND=throughput \
    ${HF_TOKEN:+-e HF_TOKEN=$HF_TOKEN} \
    -p "$PORT:$PORT" \
    "$VLLM_IMAGE" \
    vllm $args >> "$LOG" 2>&1 \
    || die "docker run failed (see $LOG)"
  write_meta container
}

# --- subcommands ------------------------------------------------------------
do_start() {
  if health; then
    log "already serving '$SERVED_NAME' on :$PORT (healthy) — no-op"
    return 0
  fi
  local engine; engine="$(resolve_engine)"
  log "engine=$engine model=$MODEL served_name=$SERVED_NAME parser=$TOOL_PARSER port=$PORT"
  case "$engine" in
    venv)      start_venv ;;
    container) start_container ;;
  esac
  log "launch issued. Weights may need to download/load before /v1/models is healthy."
  log "Poll: bash serve_model.sh status   (or curl http://127.0.0.1:$PORT/v1/models)"
}

do_stop() {
  log "stopping '$SESSION' (tmux + container if present); weights cache kept"
  tmux kill-session -t "$SESSION" 2>/dev/null && log "tmux session killed" || true
  if have docker; then docker rm -f "$SESSION" >/dev/null 2>&1 && log "container removed" || true; fi
  # Belt-and-suspenders: reap a stray vllm process bound to our port.
  pkill -f "vllm serve $MODEL" 2>/dev/null || true
  log "stopped."
}

do_status() {
  local alive_tmux="no" alive_docker="no" healthy="no"
  tmux has-session -t "$SESSION" 2>/dev/null && alive_tmux="yes" || true
  if have docker && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$SESSION"; then
    alive_docker="yes"
  fi
  health && healthy="yes" || true
  echo "##serve"
  echo "session=$SESSION"
  echo "tmux_alive=$alive_tmux"
  echo "container_alive=$alive_docker"
  echo "healthy=$healthy"
  echo "served_name=$SERVED_NAME"
  echo "port=$PORT"
  echo "##models"
  curl -fsS --max-time 5 "http://127.0.0.1:$PORT/v1/models" 2>/dev/null || echo "(no /v1/models response)"
  echo ""
  echo "##gpu"
  nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader 2>/dev/null || echo "(no nvidia-smi)"
  echo "##meta"
  cat "$META" 2>/dev/null || echo "(no meta)"
}

case "$CMD" in
  start)  do_start ;;
  stop)   do_stop ;;
  status) do_status ;;
  health) health && { echo "healthy"; exit 0; } || { echo "unhealthy"; exit 1; } ;;
  logs)   tail -c "${LOG_BYTES:-20000}" "$LOG" 2>/dev/null || echo "(no log at $LOG)" ;;
  *) die "unknown subcommand '$CMD' (start|stop|status|health|logs)" ;;
esac
