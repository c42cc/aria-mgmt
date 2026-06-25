"""Capture a rendered page + an INDEPENDENT Gemini screenshot-correctness verdict.

The visual half of "verify as close to the end user as possible." Headless Chrome
renders a URL (http(s) or file://) and screenshots it; the ONE Gemini judge
(src/home_verify.gemini_verdict) reads the frame and answers PASS/FAIL on whether
it matches intent. This is the SAME judge primitive home_verify uses for HA camera
frames — one Gemini-judge home, two capture sources (browser render, camera), per
the recombine-the-primitive doctrine. No second engine.

Tiering (v3 §4.0): this expensive visual check is a candidate-gate; cheap
structural checks run first elsewhere. The Gemini verdict is the recorded,
calibrated OBSERVER of richness/correctness — a deterministic arbiter gates live.

    python -m src.visual_verify --url file:///abs/viz.html --question "Is a green circle visible?"

Loud + no fallback: a missing Chrome or a broken capture raises (a broken
instrument must never read as a confident pass).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .config import config
from .home_verify import gemini_verdict  # the ONE Gemini judge (reused, not duplicated)

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class CaptureError(RuntimeError):
    """The capture system failed. Loud + typed — never a silent empty frame."""


def capture(url: str, out_path: Path, *, width: int = 1280, height: int = 900,
            settle_ms: int = 1500) -> Path:
    """Screenshot `url` with headless Chrome (WebGL via SwiftShader/ANGLE — no GPU
    needed for the correctness frame). Raises CaptureError on any empty/failed shot."""
    if not Path(CHROME).exists():
        raise CaptureError(f"Chrome not found at {CHROME!r} — the capture system needs it installed.")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        CHROME, "--headless=new", "--hide-scrollbars", "--force-color-profile=srgb",
        f"--window-size={width},{height}", f"--screenshot={out_path}",
        f"--virtual-time-budget={settle_ms}", url,
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired as e:
        raise CaptureError(f"headless Chrome timed out capturing {url!r}: {e}") from e
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise CaptureError(f"no screenshot produced for {url!r} (chrome said: {(p.stderr or '')[-300:]})")
    return out_path


def verify(url: str, question: str, *, out_path: Path | None = None) -> tuple[bool, str, Path]:
    """Capture `url` and return (pass, reason, screenshot_path) from the one Gemini judge."""
    if out_path is None:
        out_path = config.data_dir / "visual" / f"shot_{int(time.time())}.png"
    png = capture(url, out_path)
    ok, reason = gemini_verdict(png.read_bytes(), question)
    return ok, reason, png


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="http(s):// or file:// URL to render")
    ap.add_argument("--question", required=True, help="the visual success condition to confirm")
    ap.add_argument("--out", default="", help="screenshot path (default data/visual/shot_<ts>.png)")
    args = ap.parse_args()
    try:
        ok, reason, png = verify(args.url, args.question,
                                 out_path=Path(args.out) if args.out else None)
    except CaptureError as e:
        print(f"FATAL (capture): {e}", file=sys.stderr)
        return 2
    print(f"url    : {args.url}")
    print(f"frame  : {png}")
    print(f"gemini : {'PASS' if ok else 'FAIL'} — {reason}")
    print(f"\n=> {'CORRECT' if ok else 'NOT CORRECT'} per the visual judge.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
