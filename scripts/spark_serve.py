#!/usr/bin/env python3
"""DGX Spark — Section C serve acceptance (capture + Gemini verify) + model bench.

Proves the LOCAL BRAIN good states, each twice: a machine assertion over the real
/v1/messages (or /v1/models, nvidia-smi) output AND an independent Gemini reading
of a macOS-Terminal screenshot of that same output. A disagreement is a loud FAIL.
This is the runtime contract for "Aria's agent loop can run on a model served
from the Spark" — most importantly the tool_use round-trip, the #1 OSS risk.

All logic lives in src/spark.py (the serve helpers + pure assertions); this CLI
only wires the curl/ssh runners to the shared capture + Gemini machinery, exactly
as scripts/spark_cluster.py does for Section B.

USAGE
  # verify a node that is ALREADY serving (default model name 'local-brain'):
  .venv/bin/python scripts/spark_serve.py --node spark1

  # start the default model, wait for healthy, then verify:
  .venv/bin/python scripts/spark_serve.py --node spark1 --start

  # only the tool-call gate (the one that decides if the loop can work):
  .venv/bin/python scripts/spark_serve.py --node spark1 --only serve_toolcall

  # bench both candidates behind the same served name and recommend a default
  # (tool-call reliability first, then latency). Heavy: each model downloads/loads.
  .venv/bin/python scripts/spark_serve.py --node spark1 --bench

Artifacts: data/spark/serve/ (one PNG per gate + serve.json; bench.json for --bench).
No silent fallbacks: a red gate prints the fix and the harness exits non-zero.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from src import spark  # noqa: E402
from src.spark import (  # noqa: E402
    Gate,
    display_and_capture,
    ensure_terminal_window,
    gemini_verdict,
)


def _build_gates(node: str, port: int):
    """(Gate, run_lambda) pairs. Each run returns (combined_output, rc)."""
    return [
        (Gate("serve_models", "/v1/models lists the served brain",
              "curl /v1/models", "curl http://<node>:PORT/v1/models",
              spark.assert_serve_models,
              "Does the terminal show a /v1/models JSON response that includes a model "
              f"named '{spark.SERVED_NAME}' and an HTTP_STATUS of 200?", ""),
         lambda: spark.models_curl(node, port=port)),
        (Gate("serve_chat", "/v1/messages returns an assistant reply",
              "curl /v1/messages (plain)", "POST /v1/messages (plain prompt)",
              spark.assert_serve_chat,
              "Does the terminal show a /v1/messages JSON response with an assistant text "
              "reply (a content block of type text) and HTTP_STATUS 200?", ""),
         lambda: spark.messages_curl(node, spark.messages_payload_plain(), port=port)),
        (Gate("serve_toolcall", "/v1/messages emits a parseable tool_use",
              "curl /v1/messages (tool)", "POST /v1/messages (forces a tool call)",
              spark.assert_serve_toolcall,
              "Does the terminal show a /v1/messages JSON response that contains a content "
              "block of type 'tool_use' for the tool 'get_weather' AND a stop_reason of "
              "'tool_use', with HTTP_STATUS 200?",
              "Pick a model+--tool-call-parser combo whose tool calls parse (e.g. gpt-oss "
              "'openai' parser, Qwen3 'hermes'); re-serve with serve_model.sh."),
         lambda: spark.messages_curl(node, spark.messages_payload_toolcall(), port=port)),
        (Gate("serve_cache_control", "vLLM accepts the loop's cache_control blocks",
              "curl /v1/messages (cache_control)", "POST /v1/messages with cache_control:ephemeral",
              spark.assert_serve_cache_control,
              "Does the terminal show a /v1/messages response with HTTP_STATUS 200 and a "
              "non-empty content reply (the server accepted the cache_control fields)?",
              "If HTTP 400: add a per-process skip of the cache_control markers in "
              "src/tools.py _cache_marked_* for the local-brain process."),
         lambda: spark.messages_curl(node, spark.messages_payload_cache_control(), port=port)),
        (Gate("serve_gpu", "model weights resident on the GB10",
              "nvidia-smi memory.used", "nvidia-smi --query-gpu=name,memory.used,memory.total",
              spark.assert_serve_gpu,
              "Does the terminal show nvidia-smi reporting an NVIDIA GB10 GPU with a large "
              "amount of memory in use (tens of GB), indicating a model is loaded?", ""),
         lambda: spark.ssh_probe(
             node, "nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader")),
    ]


def _run_gates(node: str, port: int, only: list[str] | None) -> list[dict]:
    out_dir = REPO_ROOT / "data" / "spark" / "serve"
    out_dir.mkdir(parents=True, exist_ok=True)
    gates = _build_gates(node, port)
    if only:
        wanted = {g.strip() for g in only if g.strip()}
        gates = [(g, r) for (g, r) in gates if g.id in wanted]
        if not gates:
            raise ValueError(f"--only matched no serve gates ({wanted})")
    ensure_terminal_window()

    results: list[dict] = []
    for gate, run in gates:
        print(f"\n=== {gate.id} :: {gate.title} ===", flush=True)
        out, rc = run()
        png = out_dir / f"{gate.id}.png"
        try:
            display_and_capture(node, gate, out, rc, png)
        except Exception as e:  # capture is best-effort; the machine assert is ground truth
            print(f"  capture error: {e}", flush=True)
        machine_ok, detail = gate.assert_fn(out, rc)
        print(f"  machine: {'OK' if machine_ok else 'FAIL'} (rc={rc}) — {detail}", flush=True)
        try:
            gpass, greason = gemini_verdict(png, gate.gemini_q)
        except Exception as e:
            gpass, greason = False, f"gemini error: {e}"
        print(f"  gemini : {'PASS' if gpass else 'FAIL'} — {greason}", flush=True)
        verdict = "PASS" if (machine_ok and gpass) else "FAIL"
        if machine_ok != gpass:
            print("  !! machine/gemini DISAGREEMENT — treating as FAIL", flush=True)
        print(f"  -> {verdict}", flush=True)
        results.append({
            "id": gate.id, "title": gate.title, "verdict": verdict,
            "machine_ok": machine_ok, "machine_detail": detail,
            "gemini_pass": gpass, "gemini_reason": greason,
            "rc": rc, "output": out[:800], "png": str(png),
            "fix": "" if verdict == "PASS" else gate.fix,
        })

    (out_dir / "serve.json").write_text(json.dumps({
        "node": node, "port": port, "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "summary": {"total": len(results), "pass": sum(1 for r in results if r["verdict"] == "PASS")},
        "gates": results,
    }, indent=2))
    return results


def _wait_healthy(node: str, port: int, load_timeout: float) -> bool:
    """Poll serve_status until healthy or timeout. Loud progress, no silent give-up."""
    deadline = time.monotonic() + load_timeout
    while time.monotonic() < deadline:
        st = spark.serve_status(node, port=port)
        if st.get("healthy"):
            print(f"  healthy: {st.get('served_name')} on {st.get('endpoint')}", flush=True)
            return True
        print(f"  loading… tmux={st.get('tmux_alive')} container={st.get('container_alive')} "
              f"healthy=no ({int(deadline - time.monotonic())}s left)", flush=True)
        time.sleep(15)
    return False


def _toolcall_passes(node: str, port: int, n: int) -> tuple[int, int]:
    """Run the tool-call probe n times; return (passes, total)."""
    passes = 0
    for i in range(n):
        out, rc = spark.messages_curl(node, spark.messages_payload_toolcall(), port=port)
        ok, detail = spark.assert_serve_toolcall(out, rc)
        print(f"    toolcall {i + 1}/{n}: {'PASS' if ok else 'FAIL'} — {detail}", flush=True)
        passes += 1 if ok else 0
    return passes, n


def _measure_tok_s(node: str, port: int) -> float:
    """Approximate decode tok/s from one timed generation (output_tokens / elapsed)."""
    payload = {"model": spark.SERVED_NAME, "max_tokens": 128,
               "messages": [{"role": "user", "content": "Count from 1 to 60, one number per line."}]}
    t0 = time.monotonic()
    out, _rc = spark.messages_curl(node, payload, port=port, timeout=120)
    dt = max(1e-6, time.monotonic() - t0)
    m = re.search(r'"output_tokens"\s*:\s*(\d+)', out)
    toks = int(m.group(1)) if m else 0
    return toks / dt


def _bench(node: str, port: int, candidates: list[str], n: int, load_timeout: float,
           keep: bool) -> int:
    out_dir = REPO_ROOT / "data" / "spark" / "serve"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for key in candidates:
        cfg = spark.SERVE_MODELS.get(key, {})
        print(f"\n##### BENCH: {key} ({cfg.get('hf', key)}) — {cfg.get('note', '')}", flush=True)
        start = spark.serve_start(node, model=key, port=port)
        print(f"  serve_start: ok={start.get('ok')} parser={start.get('tool_parser')} "
              f"endpoint={start.get('endpoint')}", flush=True)
        if not start.get("ok"):
            rows.append({"model": key, "served": False, "error": start.get("error", "")[:300]})
            continue
        if not _wait_healthy(node, port, load_timeout):
            rows.append({"model": key, "served": False, "error": "did not become healthy in time"})
            if not keep:
                spark.serve_stop(node, port=port)
            continue
        passes, total = _toolcall_passes(node, port, n)
        tok_s = _measure_tok_s(node, port)
        print(f"  -> toolcall {passes}/{total}, ~{tok_s:.1f} tok/s", flush=True)
        rows.append({"model": key, "hf": cfg.get("hf", key), "parser": cfg.get("parser"),
                     "served": True, "toolcall_pass": passes, "toolcall_total": total,
                     "tok_s": round(tok_s, 1)})
        if not keep:
            spark.serve_stop(node, port=port)

    # Recommend: max tool-call pass-rate first, then fastest. Tool reliability is
    # non-negotiable — a fast model that cannot tool-call is useless to the loop.
    eligible = [r for r in rows if r.get("served") and r.get("toolcall_total")]
    winner = None
    if eligible:
        winner = sorted(
            eligible,
            key=lambda r: (r["toolcall_pass"] / r["toolcall_total"], r.get("tok_s", 0.0)),
            reverse=True,
        )[0]

    (out_dir / "bench.json").write_text(json.dumps({
        "node": node, "port": port, "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "n_per_model": n, "rows": rows,
        "recommended": winner["model"] if winner else None,
    }, indent=2))

    print("\n" + "=" * 60, flush=True)
    print("SPARK SECTION-C MODEL BENCH", flush=True)
    for r in rows:
        if r.get("served"):
            print(f"  {r['model']:16s} toolcall {r['toolcall_pass']}/{r['toolcall_total']}  "
                  f"~{r.get('tok_s', 0)} tok/s  parser={r.get('parser')}", flush=True)
        else:
            print(f"  {r['model']:16s} NOT SERVED — {r.get('error', '')}", flush=True)
    if winner:
        print(f"\n  RECOMMEND default: {winner['model']} "
              f"(toolcall {winner['toolcall_pass']}/{winner['toolcall_total']}, "
              f"~{winner.get('tok_s')} tok/s). Set CLAUDE_MODEL={spark.SERVED_NAME} and serve it.", flush=True)
    else:
        print("\n  NO eligible model — none served + tool-called. See bench.json.", flush=True)
    print("=" * 60, flush=True)
    return 0 if winner else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--node", default="spark1", help="ssh alias / host (default spark1)")
    ap.add_argument("--port", type=int, default=spark.SERVE_PORT)
    ap.add_argument("--start", action="store_true", help="serve the default model + wait healthy before verifying")
    ap.add_argument("--model", default=None, help="model key/HF id to start (with --start)")
    ap.add_argument("--only", default="", help="comma-separated serve gate ids")
    ap.add_argument("--bench", action="store_true", help="bench all candidates + recommend a default")
    ap.add_argument("--candidates", default=",".join(spark.SERVE_MODELS.keys()),
                    help="comma-separated model keys to bench")
    ap.add_argument("--n", type=int, default=5, help="tool-call probes per model in --bench")
    ap.add_argument("--load-timeout", type=float, default=1800.0,
                    help="seconds to wait for a model to become healthy")
    ap.add_argument("--keep", action="store_true", help="leave the last benched model serving")
    ap.add_argument("--stop", action="store_true", help="tear the server down (teardown) and exit")
    args = ap.parse_args()

    if args.stop:
        res = spark.serve_stop(args.node, port=args.port)
        print(f"[serve_stop] ok={res.get('ok')} node={res.get('node')}", flush=True)
        print((res.get("detail") or "").strip(), flush=True)
        return 0 if res.get("ok") else 1

    if args.bench:
        cands = [c.strip() for c in args.candidates.split(",") if c.strip()]
        return _bench(args.node, args.port, cands, args.n, args.load_timeout, args.keep)

    if args.start:
        res = spark.serve_start(args.node, model=args.model, port=args.port)
        print(f"[serve_start] ok={res.get('ok')} model={res.get('model')} "
              f"parser={res.get('tool_parser')} endpoint={res.get('endpoint')}", flush=True)
        if not res.get("ok"):
            print(f"FATAL: serve_start failed: {res.get('error')}", file=sys.stderr)
            return 2
        if not _wait_healthy(args.node, args.port, args.load_timeout):
            print("FATAL: model did not become healthy in time", file=sys.stderr)
            return 2

    only = [g.strip() for g in args.only.split(",") if g.strip()] or None
    try:
        results = _run_gates(args.node, args.port, only)
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 2

    passed = [r for r in results if r["verdict"] == "PASS"]
    failed = [r for r in results if r["verdict"] != "PASS"]
    print("\n" + "=" * 60, flush=True)
    print(f"SPARK SECTION-C SERVE ACCEPTANCE :: {args.node}", flush=True)
    for r in results:
        print(f"  [{r['verdict']}] {r['id']:20s} {r['machine_detail']}", flush=True)
        if r["verdict"] != "PASS" and r["fix"]:
            print(f"          fix: {r['fix']}", flush=True)
    print(f"\n  {len(passed)}/{len(results)} serve gates green. "
          f"Report: {REPO_ROOT / 'data' / 'spark' / 'serve' / 'serve.json'}", flush=True)
    print("=" * 60, flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
