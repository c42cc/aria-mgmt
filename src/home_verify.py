"""Phase-4 physical-state verification — capture + an independent Gemini read.

ABSENCES.md keeps the capture+Gemini primitive deleted as a CODE gate and brings
it back ONLY here, at Phase 4, for genuine physical/visual state: "did the garage
actually close?", "are the lights really on?" A Home Assistant entity's reported
state is one proof (the dispatcher already verifies that); a camera frame read by
Gemini is the SECOND, independent proof that the physical world matches — the
honest bar for a body that moves atoms.

Run (needs a live HA + an exposed camera + GEMINI_API_KEY):
    python -m src.home_verify --camera camera.garage --question "Is the garage door fully closed?"

Honest when unconfigured: prints the one fix and exits non-zero. No silent pass.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time

from . import homeassistant
from .config import config

_GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]


def gemini_verdict(image: bytes, question: str) -> tuple[bool, str]:
    """Independent visual read of a real frame. (pass, reason). Loud on failure —
    a broken verifier must never read as a confident pass."""
    if not config.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not set — cannot run the Phase-4 visual verify.")
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=config.gemini_api_key)
    img = types.Part.from_bytes(data=image, mime_type="image/jpeg")
    prompt = (
        "You are verifying the physical state of a smart home from a camera frame. "
        f"Question: {question}\n"
        'Return STRICT JSON only: {"pass": true|false, "reason": "<one short sentence>"}. '
        "Set pass=true ONLY if the frame clearly shows the success condition."
    )
    transient = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "overloaded", "500", "INTERNAL", "deadline", "timeout")
    last = ""
    for model in _GEMINI_MODELS:
        for attempt in range(3):
            try:
                resp = client.models.generate_content(
                    model=model, contents=[prompt, img],
                    config=types.GenerateContentConfig(temperature=0.0),
                )
                text = (resp.text or "").strip()
                m = re.search(r"\{.*\}", text, re.DOTALL)
                if m:
                    data = json.loads(m.group(0))
                    return bool(data.get("pass")), f"[{model}] {str(data.get('reason', ''))[:180]}"
                last = f"{model}: unparseable ({text[:80]!r})"
                break
            except Exception as e:  # transient model demand is ours to absorb, not to blame
                msg = str(e)
                last = f"{model}: {type(e).__name__}: {msg[:120]}"
                if not any(k in msg for k in transient):
                    break
                time.sleep(2 * (2 ** attempt))
    return False, f"all gemini models failed: {last}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", required=True, help="HA camera entity id (e.g. camera.garage)")
    ap.add_argument("--question", required=True, help="the physical success condition to confirm")
    args = ap.parse_args()

    if not homeassistant.configured():
        print(homeassistant.NOT_CONFIGURED, file=sys.stderr)
        return 2

    try:
        frame = homeassistant.camera_snapshot(args.camera)
    except homeassistant.HomeAssistantError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 2

    out_dir = config.data_dir / "home"
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / f"{args.camera.replace('.', '_')}.jpg"
    png.write_bytes(frame)

    # Both proofs: HA's reported state AND the independent visual read.
    reported = homeassistant.get_state(args.camera)
    reported_state = str((reported or {}).get("state", "?"))
    ok, reason = gemini_verdict(frame, args.question)

    print(f"camera : {args.camera} (HA reports state={reported_state!r})")
    print(f"frame  : {png}")
    print(f"gemini : {'PASS' if ok else 'FAIL'} — {reason}")
    print(f"\n=> {'CONFIRMED' if ok else 'NOT CONFIRMED'} physically.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
