"""`make doctor` — the ONE observability home for the whole dev environment.

Every plane, one glanceable screen (and `--json` for machines): the Mind
(local inference), the Hands (cells), the Floor (state of record), Home
Assistant (the management surface), the Cloud (judgment), plus today's spend and
the last real request. This is how "all failures are observable to you" is real,
not aspirational.

Discipline: each probe is LOUD and names the one fix; a plane that is down or
absent says so plainly (never a green-by-omission). A connectivity failure is
OURS to surface — never blamed on the node, the hub, or the network. Probes run
in parallel with short timeouts so the screen answers fast.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

from . import floor, spark
from .config import config

# Plane → box (the Mind serves; the Hands run cells).
MIND_NODE = "spark1"
HANDS_NODE = "spark2"


@dataclass
class Plane:
    name: str
    state: str  # "ok" | "down" | "absent" | "not_configured"
    detail: str
    fix: str = ""

    @property
    def glyph(self) -> str:
        return {"ok": "OK ", "down": "DOWN", "absent": "ABST", "not_configured": "----"}.get(self.state, "????")


def _probe_mind() -> Plane:
    """The Mind = vLLM local-brain at SPARK_BASE_URL (the real dispatcher path)."""
    if not config.spark_base_url:
        return Plane("mind", "not_configured", "SPARK_BASE_URL unset", "set SPARK_BASE_URL to the Mind's vLLM endpoint")
    url = f"{config.spark_base_url}/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            body = json.loads(r.read().decode() or "{}")
        served = [m.get("id") for m in body.get("data", [])] or ["(none)"]
        ok = config.spark_model in served or any(config.spark_model in str(s) for s in served)
        return Plane(
            "mind", "ok" if ok else "down",
            f"serving {served} at {config.spark_base_url}" if ok
            else f"reachable but {config.spark_model!r} not served (has {served})",
            "" if ok else f"serve it: make spark-serve SPARK_NODE={MIND_NODE}",
        )
    except (urllib.error.URLError, OSError, ValueError) as e:
        return Plane(
            "mind", "down",
            f"not serving at {config.spark_base_url} ({type(e).__name__}: {e})",
            f"bring the Mind up: make spark-serve SPARK_NODE={MIND_NODE}",
        )


def _probe_hands() -> Plane:
    """The Hands = spark2 reachable over Tailscale SSH with Claude Code authed."""
    try:
        out, rc = spark.ssh_probe(
            HANDS_NODE,
            ". ~/.config/spark/env.sh 2>/dev/null; echo HOST=$(hostname); "
            "claude --version 2>/dev/null | head -1 || echo NO_CLAUDE",
            timeout=10,
        )
    except Exception as e:  # our connectivity to surface, never the node's fault
        return Plane("hands", "down", f"ssh to {HANDS_NODE} failed ({type(e).__name__}: {e})",
                     f"check the tailnet: tailscale status | grep {HANDS_NODE}")
    if rc != 0:
        return Plane("hands", "down", f"{HANDS_NODE} unreachable (rc={rc}): {out[:120]}",
                     f"check the tailnet: tailscale status | grep {HANDS_NODE}")
    if "NO_CLAUDE" in out:
        return Plane("hands", "down", f"{HANDS_NODE} reachable but Claude Code not on PATH",
                     f"provision: python scripts/spark_serve.py is not it — run ops/spark/setup_node.sh on {HANDS_NODE}")
    return Plane("hands", "ok", f"{HANDS_NODE} reachable, claude present ({out.splitlines()[-1].strip()})")


def _probe_floor() -> Plane:
    s = floor.status()
    return Plane("floor", "ok" if s.present else "absent", s.detail,
                 "" if s.present else "mount the NAS and set FLOOR_ROOT to it")


def _probe_ha() -> Plane:
    """Home Assistant = the management surface. Honest about not-configured / down."""
    from . import homeassistant
    if not homeassistant.configured():
        return Plane("ha", "not_configured",
                     "HASS_URL/HASS_TOKEN not both set", "stand up HA on the Mind, then set HASS_TOKEN (G8)")
    try:
        homeassistant.get_states()
        return Plane("ha", "ok", f"reachable at {config.hass_url}")
    except homeassistant.HomeAssistantError as e:
        return Plane("ha", "down", f"{e}", "bring the hub up on the Mind; it is ours to reach, not HA's to blame")


def _probe_cloud() -> Plane:
    if not config.anthropic_api_key:
        return Plane("cloud", "not_configured", "ANTHROPIC_API_KEY unset", "set ANTHROPIC_API_KEY in .env")
    return Plane("cloud", "ok", f"key present; judgment={config.reasoning_model}")


def _spend_and_last() -> tuple[float, str]:
    from . import outcome_log
    rows = outcome_log.read_all()
    today = __import__("time").strftime("%Y-%m-%d")
    spend = sum(float(r.get("cost_usd") or 0.0) for r in rows if str(r.get("ts", "")).startswith(today))
    last = rows[-1] if rows else None
    last_str = (
        f"{last.get('loop_id')}: {'delivered' if last.get('delivered') else 'NOT delivered'} — "
        f"{str(last.get('summary',''))[:80]}"
        if last else "(no requests logged yet)"
    )
    return spend, last_str


def probe() -> dict:
    """Probe every plane in parallel; return the machine-readable report."""
    probes = {
        "mind": _probe_mind, "hands": _probe_hands, "floor": _probe_floor,
        "ha": _probe_ha, "cloud": _probe_cloud,
    }
    with ThreadPoolExecutor(max_workers=len(probes)) as ex:
        results = {name: fut.result() for name, fut in {n: ex.submit(f) for n, f in probes.items()}.items()}
    spend, last = _spend_and_last()
    return {
        "planes": {name: asdict(p) for name, p in results.items()},
        "spend_today_usd": round(spend, 4),
        "last_request": last,
        "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def render(report: dict) -> str:
    lines = ["", "  DEV-ENVIRONMENT DOCTOR — the single pane", "  " + "-" * 46]
    order = ["mind", "hands", "floor", "ha", "cloud"]
    for name in order:
        p = report["planes"][name]
        line = f"  [{Plane(**p).glyph}] {name:<6} {p['detail']}"
        lines.append(line)
        if p.get("fix"):
            lines.append(f"         fix: {p['fix']}")
    lines.append("  " + "-" * 46)
    lines.append(f"  spend today: ${report['spend_today_usd']}   |   last: {report['last_request']}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    report = probe()
    if "--json" in sys.argv:
        print(json.dumps(report, indent=2))
    else:
        print(render(report))
    # Exit 0 always: the doctor REPORTS; it does not gate. A down plane is shown,
    # not hidden — the gate/use-time checks are where a failure blocks.
    return 0


if __name__ == "__main__":
    sys.exit(main())
