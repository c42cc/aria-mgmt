"""Verify — the only intelligence in the pipeline.

Two separable things, never conflated:

1. The COMPLETENESS GATE (the trust bit). Provable from KB-sized header
   range-reads at any scale, and it NEVER executes repo code:
     - manifest_closure: every tensor the index promises is present in exactly one
       shard header; every referenced shard is present; no missing, no extra.
     - identity_present: the model is self-describing (config captured).
     - tokenizer_consistency: a declared tokenizer has its data files captured.
     - code_capture: every `auto_map`-referenced `.py` is captured and the named
       class actually exists in it (checked by AST parse, not by importing it).
   If the gate fails, the artifact is NOT trusted (exit 2).

2. The LOADABILITY TIER (recorded, not a gate). For standard architectures we
   instantiate the graph on the `meta` device (zero weight memory — feasible even
   at 1T) and cross-check keys. For custom-code models we do NOT run their Python;
   the authoritative load is the loader oracle at `restore --smoke-test` on
   hardware that fits the bytes. Whatever we did or deferred is written down.
"""

from __future__ import annotations

import ast
import logging
import os
from pathlib import Path

from . import VerificationError
from .acquire import AcquireProbe
from .config import Settings
from .manifest import Verification, utcnow

log = logging.getLogger("modelvault.verify")

_TOKENIZER_DATA_FILES = (
    "tokenizer.json", "tokenizer.model", "spiece.model", "vocab.json", "vocab.txt",
    "merges.txt", "tiktoken.model",
)


def run_verification(probe: AcquireProbe, settings: Settings) -> Verification:
    v = Verification()
    checks: list[str] = []

    _closure(probe, checks)
    _identity(probe, checks)
    _tokenizer_consistency(probe, checks)
    _code_capture(probe, checks)

    # Gate passed -> the backup is provably complete and self-describing.
    v.status = "verified"
    v.completeness_checks = checks

    level, method, executed = _loadability(probe, settings)
    v.loadability_level = level
    v.loadability_method = method
    v.loadability_executed_repo_code = executed
    v.verified_at = utcnow()
    v.tool_versions = _tool_versions()
    return v


# ----------------------------------------------------------------- gate checks
def _closure(probe: AcquireProbe, checks: list[str]) -> None:
    if probe.weight_format == "safetensors":
        present = {f.path for f in probe.files}
        if probe.index is not None:
            weight_map = probe.index.get("weight_map")
            if not isinstance(weight_map, dict) or not weight_map:
                raise VerificationError("safetensors index has no weight_map")
            promised = set(weight_map.keys())
            shards = set(weight_map.values())
            seen: set[str] = set()
            for shard in sorted(shards):
                if shard not in present:
                    raise VerificationError(f"index references missing shard: {shard}")
                if shard not in probe.headers:
                    raise VerificationError(f"could not read header for shard: {shard}")
                tensors = _header_tensors(probe.headers[shard])
                dup = seen & tensors
                if dup:
                    raise VerificationError(f"tensor in multiple shards: {sorted(dup)[:3]}")
                seen |= tensors
            missing = promised - seen
            extra = seen - promised
            if missing:
                raise VerificationError(
                    f"{len(missing)} tensor(s) promised by index but absent from shards: {sorted(missing)[:3]}"
                )
            if extra:
                raise VerificationError(
                    f"{len(extra)} tensor(s) in shards but not in index: {sorted(extra)[:3]}"
                )
            checks.append("manifest_closure")
        else:
            shards = [f for f in probe.files if f.path.endswith(".safetensors")]
            if not shards:
                raise VerificationError("no .safetensors files found")
            for f in shards:
                if f.path not in probe.headers:
                    raise VerificationError(f"could not read header for {f.path}")
                if not _header_tensors(probe.headers[f.path]):
                    raise VerificationError(f"{f.path}: header declares zero tensors")
            checks.append("single_file_closure")
    elif probe.weight_format == "gguf":
        if not any(f.path.endswith(".gguf") for f in probe.files):
            raise VerificationError("gguf format detected but no .gguf file present")
        checks.append("gguf_presence")
    else:
        # Non-safetensors weights: completeness is presence + a pinned hash for
        # every weight file. Weaker (no tensor-level proof) and recorded as such.
        weights = [f for f in probe.files if Path(f.path).suffix.lower() in
                   {".bin", ".pt", ".pth", ".msgpack", ".h5", ".onnx", ".ckpt"}]
        if not weights:
            raise VerificationError("no recognizable weight files found")
        for f in weights:
            if not f.sha256:
                raise VerificationError(f"{f.path}: no pinned hash available for non-safetensors weight")
        checks.append("presence_hash")


def _identity(probe: AcquireProbe, checks: list[str]) -> None:
    if probe.detected_library == "transformers" and not probe.embedded_identity.get("config.json"):
        raise VerificationError("config.json missing — model is not self-describing")
    checks.append("identity_present")


def _tokenizer_consistency(probe: AcquireProbe, checks: list[str]) -> None:
    tok_cfg = probe.embedded_identity.get("tokenizer_config.json")
    if not tok_cfg:
        return  # no tokenizer declared (e.g. a detection model) — nothing to prove
    names = {Path(f.path).name for f in probe.files}
    has_data = any(d in names for d in _TOKENIZER_DATA_FILES)
    # A custom tokenizer (auto_map) may carry its data via code/assets — accept it
    # only if the referenced .py is present (proven by code_capture below).
    custom_tok = bool(probe.auto_map and any("Tokenizer" in k for k in probe.auto_map))
    if not has_data and not custom_tok:
        raise VerificationError(
            "tokenizer_config.json present but no tokenizer data file captured "
            f"(looked for {', '.join(_TOKENIZER_DATA_FILES)})"
        )
    checks.append("tokenizer_consistency")


def _code_capture(probe: AcquireProbe, checks: list[str]) -> None:
    if not probe.auto_map:
        return
    for key, target in probe.auto_map.items():
        if not isinstance(target, str):
            continue
        ref = target.split("--")[-1]  # strip optional "repo--" prefix
        module, _, cls = ref.rpartition(".")
        if not module or not cls:
            raise VerificationError(f"auto_map[{key}] is malformed: {target!r}")
        module_file = module.split(".")[-1] + ".py"
        local = probe.small_files.get(module_file) or _match_py(probe, module_file)
        if not local:
            raise VerificationError(
                f"trust_remote_code: auto_map[{key}] needs {module_file} but it was not captured"
            )
        if not _class_defined(local, cls):
            raise VerificationError(
                f"trust_remote_code: class {cls} not found in captured {module_file}"
            )
    checks.append("code_capture")


# ----------------------------------------------------------------- loadability
def _loadability(probe: AcquireProbe, settings: Settings) -> tuple[str, str, bool]:
    if probe.trust_remote_code:
        # We refuse to execute untrusted repo code here. Structural proof only;
        # the real load happens at restore on capable hardware.
        return ("structural_only", "ast-structural (repo code captured, not executed)", False)

    if probe.weight_format != "safetensors" or probe.detected_library != "transformers":
        return ("none", f"no standard loader for {probe.weight_format}/{probe.detected_library}", False)

    try:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        from transformers import AutoConfig, AutoModel  # heavy; lazy
        from accelerate import init_empty_weights

        cfg = AutoConfig.from_pretrained(probe.tmp_dir, local_files_only=True, trust_remote_code=False)
        with init_empty_weights():
            model = AutoModel.from_config(cfg)
        model_keys = {n for n, _ in model.named_parameters()} | {n for n, _ in model.named_buffers()}
        header_tensors: set[str] = set()
        for h in probe.headers.values():
            header_tensors |= _header_tensors(h)
        unexpected = header_tensors - model_keys
        params = {n for n, _ in model.named_parameters()}
        missing = params - header_tensors
        tied = set(getattr(model, "_tied_weights_keys", None) or [])
        missing -= tied
        if not missing and not unexpected:
            return ("skeleton_ok", "transformers-skeleton (meta device)", True)
        return (
            "skeleton_mismatch",
            f"transformers-skeleton: {len(missing)} missing, {len(unexpected)} unexpected",
            True,
        )
    except Exception as e:  # recorded, not a gate failure — closure already passed
        log.warning("skeleton load could not run: %s", e)
        return ("skeleton_error", f"{type(e).__name__}: {str(e)[:160]}", False)


# ---------------------------------------------------------------------- helpers
def _header_tensors(header_obj: dict) -> set[str]:
    header = header_obj.get("header", {})
    return {k for k in header.keys() if k != "__metadata__"}


def _match_py(probe: AcquireProbe, module_file: str) -> str | None:
    for path, local in probe.small_files.items():
        if Path(path).name == module_file:
            return local
    return None


def _class_defined(py_path: str, cls: str) -> bool:
    try:
        tree = ast.parse(Path(py_path).read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return False
    return any(isinstance(node, ast.ClassDef) and node.name == cls for node in ast.walk(tree))


def _tool_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    try:
        import huggingface_hub

        versions["huggingface_hub"] = huggingface_hub.__version__
    except Exception:
        pass
    return versions
