"""src.judge_calibration — earn the judge's right to gate (Step 6).

Doctrine borrowed from live_visuals_4, scoped to the ONE judge (NOT the
full-spectrum eval apparatus de7bfd40 proposed — that builds measurement before
the one measurement that matters). The judge may only GATE a Task's "done" claim
once it has proven, over a labeled good/bad corpus, that it:

  1. AGREES with ground truth at >= AGREEMENT_MIN, and
  2. SEPARATES good from bad (every good scores strictly above every bad).

A calibration receipt is keyed to the build hash (judge.py is part of the build),
so changing the judge invalidates its calibration. An uncalibrated or stale judge
is REFUSED the right to gate — it advises, it does not decide.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from . import build_hash as _bh

log = logging.getLogger(__name__)

AGREEMENT_MIN = 0.9
CALIBRATION_VALIDITY_DAYS = 14

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CORPUS = os.path.join(_REPO, "evals", "calibration_corpus.json")
_RECEIPTS_DIR = os.path.join(_REPO, "data", "receipts")


# --- pure gates (unit-tested without any API) -----------------------------

def label_matches_verdict(label: str, verdict: str) -> bool:
    """A 'good' record should be judged correct; a 'bad' record should be judged
    anything-but-correct (degraded / failed / unverified)."""
    if label == "good":
        return verdict == "correct"
    return verdict != "correct"


def compute_agreement(results: list[dict]) -> float:
    """Fraction of corpus entries where the judge's verdict matches the label."""
    if not results:
        return 0.0
    hits = sum(1 for r in results if label_matches_verdict(r["label"], r["verdict"]))
    return hits / len(results)


def compute_separation(results: list[dict]) -> bool:
    """True iff every good entry scored strictly above every bad entry. A
    threshold over un-separated scores is meaningless, so this is a hard gate."""
    goods = [r["score"] for r in results if r["label"] == "good"]
    bads = [r["score"] for r in results if r["label"] == "bad"]
    if not goods or not bads:
        return False  # separation over an empty side is vacuous
    return min(goods) > max(bads)


def evaluate_calibration(results: list[dict]) -> dict:
    """Pure: fold judged results -> calibration verdict. `results` is a list of
    {id, label, verdict, score}. Both gates must pass."""
    agreement = compute_agreement(results)
    separation = compute_separation(results)
    passed = agreement >= AGREEMENT_MIN and separation
    return {
        "agreement": agreement,
        "agreement_min": AGREEMENT_MIN,
        "separation": separation,
        "passed": passed,
        "n": len(results),
    }


# --- receipt residency + freshness ----------------------------------------

def _receipt_path(build_hash: str) -> str:
    return os.path.join(_RECEIPTS_DIR, f"calibration_{build_hash}.json")


def latest_calibration() -> dict | None:
    """The calibration receipt for the CURRENT build, or None."""
    path = _receipt_path(_bh.compute_build_hash())
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def is_calibrated(now: datetime | None = None) -> bool:
    """True iff there is a receipt FOR THIS BUILD that passed and is fresh.
    Refuse-to-trust: anything else means the judge may not gate."""
    receipt = latest_calibration()
    if not receipt or not receipt.get("passed"):
        return False
    ts = receipt.get("ts")
    if not ts:
        return False
    try:
        when = datetime.fromisoformat(ts)
    except ValueError:
        return False
    now = now or datetime.now(timezone.utc)
    age_days = (now - when).total_seconds() / 86400.0
    return age_days <= CALIBRATION_VALIDITY_DAYS


# --- the live run (real judge API; deferred like the live meter) -----------

def _load_corpus() -> dict:
    with open(_CORPUS) as f:
        return json.load(f)


async def calibrate() -> dict:
    """Run the live judge over the labeled corpus and write a build-hash-keyed
    calibration receipt. Real Gemini calls — run via `make eval-calibrate`."""
    from . import judge

    corpus = _load_corpus()
    entries = corpus["entries"]
    spec = judge.load_spec(corpus["product"])
    if not spec:
        raise RuntimeError(f"no correctness spec for product {corpus['product']!r}")

    results: list[dict] = []
    for e in entries:
        verdict = await judge.evaluate(spec, e["record"], corpus["product"], f"cal:{e['id']}")
        results.append({
            "id": e["id"],
            "label": e["label"],
            "verdict": verdict.verdict,
            "score": verdict.score,
        })

    summary = evaluate_calibration(results)
    build_hash = _bh.compute_build_hash()
    receipt = {
        "kind": "judge_calibration",
        "build_hash": build_hash,
        "ts": datetime.now(timezone.utc).isoformat(),
        "results": results,
        **summary,
    }
    os.makedirs(_RECEIPTS_DIR, exist_ok=True)
    path = _receipt_path(build_hash)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(receipt, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    log.info(
        "judge calibration %s: agreement=%.2f separation=%s (receipt %s)",
        "PASSED" if summary["passed"] else "FAILED",
        summary["agreement"], summary["separation"], path,
    )
    return receipt


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    r = asyncio.run(calibrate())
    print(json.dumps({k: v for k, v in r.items() if k != "results"}, indent=2))
    for row in r["results"]:
        print(f"  {row['label']:4s} {row['verdict']:10s} {row['score']:.2f}  {row['id']}")
    raise SystemExit(0 if r["passed"] else 1)
