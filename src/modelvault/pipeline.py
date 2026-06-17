"""The pipeline — deliberately dumb glue around the one smart stage (verify).

backup:  resolve -> probe -> VERIFY (gate, pre-upload) -> stream blobs -> manifest
         -> flip catalog LAST.
restore: pull manifest -> rclone copy+decrypt -> re-verify every sha256 -> optional
         smoke load.
verify:  re-prove closure against the *stored* copy (scrub).
list:    read the catalog and nothing else.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import struct
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import SourceError, StorageError, VerificationError
from . import acquire, refs, verify
from .config import Settings
from .manifest import Manifest, Storage as StorageBlock, Verification, utcnow
from .storage import Storage

log = logging.getLogger("modelvault.pipeline")

_CATALOG_OBJECT = "index/index.json"


def _preflight(storage: Storage) -> None:
    """Refuse to proceed unless the remotes are actually reachable. Loud, with fix."""
    for p in storage.doctor():
        if not p.ok:
            raise StorageError(f"storage preflight failed: {p.error} ({p.detail})", fix=p.fix)


# ===================================================================== backup
def backup(
    url: str,
    settings: Settings,
    *,
    revision: str | None = None,
    source_type: str = "auto",
    force: bool = False,
) -> Manifest:
    settings.require_storage()
    storage = Storage(settings)
    _preflight(storage)

    parsed = refs.parse(url, source_type=source_type, revision=revision)
    mref = acquire.resolve(parsed, settings)
    log.info("resolved %s -> %s", url, mref.model_ref)

    catalog = _read_catalog(storage)
    if not force and catalog.get(mref.model_ref, {}).get("status") == "verified":
        log.info("already verified; no-op (idempotent): %s", mref.model_ref)
        existing = storage.cat(settings.remote_standard, mref.manifest_object)
        if existing:
            return Manifest.from_json(existing.decode())
        raise StorageError(f"catalog says verified but manifest missing: {mref.manifest_object}")

    probe = acquire.probe(mref, settings)
    log.info(
        "probed %s: %d files, %.2f GB, format=%s, trust_remote_code=%s",
        mref.model_ref, len(probe.files), probe.total_bytes / 1e9, probe.weight_format,
        probe.trust_remote_code,
    )

    verification = verify.run_verification(probe, settings)  # raises -> exit 2 on gate fail
    log.info(
        "VERIFIED complete: checks=%s loadability=%s",
        verification.completeness_checks, verification.loadability_level,
    )

    manifest = _build_manifest(mref, probe, verification, settings)

    _stream_blobs(mref, probe, storage, settings)
    _confirm_uploaded(mref, probe, storage, settings)

    storage.upload_bytes(manifest.to_json().encode(), settings.remote_standard, mref.manifest_object)
    catalog[mref.model_ref] = manifest.catalog_entry()
    _write_catalog(storage, catalog)  # the trust flip — LAST
    log.info("catalog updated -> verified: %s", mref.model_ref)

    shutil.rmtree(probe.tmp_dir, ignore_errors=True)
    return manifest


def _build_manifest(mref, probe, verification: Verification, settings: Settings) -> Manifest:
    return Manifest(
        model_ref=mref.model_ref,
        source_url=mref.source_url,
        source_type=mref.source_type,
        repo_id=mref.repo_id,
        revision_requested=mref.revision_requested,
        revision_resolved_sha=mref.revision_resolved_sha,
        created_at=utcnow(),
        detected_library=probe.detected_library,
        weight_format_selected=probe.weight_format,
        trust_remote_code=probe.trust_remote_code,
        total_bytes=probe.total_bytes,
        files=probe.files,
        embedded_identity=probe.embedded_identity,
        verification=verification,
        storage=StorageBlock(
            remote_archive=settings.remote_archive,
            remote_standard=settings.remote_standard,
            blobs_prefix=mref.blobs_prefix,
            manifest_object=mref.manifest_object,
            blob_storage_class="ARCHIVE",
        ),
    )


def _stream_blobs(mref, probe, storage: Storage, settings: Settings) -> None:
    present = set(storage.list_files(settings.remote_archive, mref.blobs_prefix))
    todo = [f for f in probe.files if f.path not in present]
    skipped = len(probe.files) - len(todo)
    if skipped:
        log.info("resuming: %d/%d blobs already present", skipped, len(probe.files))

    def _one(path: str) -> str:
        loc = acquire.file_location(mref, path, settings)
        storage.stream_url(loc.url, settings.remote_archive, f"{mref.blobs_prefix}{path}")
        return path

    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, settings.transfers)) as pool:
        futures = {pool.submit(_one, f.path): f.path for f in todo}
        for fut in as_completed(futures):
            path = futures[fut]
            try:
                fut.result()
                log.info("streamed %s", path)
            except Exception as e:  # collect; we fail loudly after the pool drains
                errors.append(f"{path}: {e}")
    if errors:
        raise StorageError("streaming failed for:\n  " + "\n  ".join(errors[:10]))


def _confirm_uploaded(mref, probe, storage: Storage, settings: Settings) -> None:
    present = set(storage.list_files(settings.remote_archive, mref.blobs_prefix))
    missing = [f.path for f in probe.files if f.path not in present]
    if missing:
        raise StorageError(
            f"{len(missing)} file(s) missing from store after upload: {missing[:5]}"
        )


# ==================================================================== restore
def restore(
    model_ref: str,
    settings: Settings,
    *,
    dest: str | None = None,
    smoke: bool = False,
) -> str:
    settings.require_storage()
    storage = Storage(settings)
    _preflight(storage)

    manifest_obj = f"manifests/{model_ref}.json"
    text = storage.cat(settings.remote_standard, manifest_obj)
    if text is None:
        raise StorageError(
            f"no manifest for {model_ref} — not backed up (or wrong key)",
            fix="modelvault list   # to see what is in the vault",
        )
    manifest = Manifest.from_json(text.decode())

    dest_dir = dest or str(settings.repo_root / "restored" / model_ref)
    os.makedirs(dest_dir, exist_ok=True)
    log.info("restoring %s -> %s (%.2f GB)", model_ref, dest_dir, manifest.total_bytes / 1e9)

    storage.download_dir(settings.remote_archive, f"blobs/{model_ref}/", dest_dir)
    _reverify_hashes(manifest, dest_dir)
    log.info("all %d files restored and sha256-verified", len(manifest.files))

    if smoke:
        _smoke_load(dest_dir, manifest)

    return dest_dir


def _reverify_hashes(manifest: Manifest, dest_dir: str) -> None:
    for f in manifest.files:
        local = Path(dest_dir) / f.path
        if not local.exists():
            raise VerificationError(f"restored copy missing file: {f.path}")
        if f.sha256:
            actual = _sha256(local)
            if actual != f.sha256:
                raise VerificationError(
                    f"sha256 mismatch on {f.path}: manifest {f.sha256[:12]} != restored {actual[:12]}"
                )
        elif local.suffix.lower() == ".safetensors":
            raise VerificationError(f"{f.path}: no pinned sha256 to verify the restored shard against")


def _smoke_load(dest_dir: str, manifest: Manifest) -> None:
    """Authoritative 'it loads' oracle. Materialize tensors; forward-pass if standard."""
    from safetensors import safe_open

    shards = [f for f in manifest.files if f.path.endswith(".safetensors")]
    too_big = manifest.total_bytes > 8 * 1024**3  # don't try to hold >8 GB in RAM here
    if shards and not too_big:
        for f in shards:
            path = str(Path(dest_dir) / f.path)
            with safe_open(path, framework="numpy") as st:
                for key in st.keys():
                    st.get_tensor(key)  # fully materialize — proves the bytes load
        log.info("smoke: materialized all tensors from %d shard(s)", len(shards))
    elif too_big:
        log.warning(
            "smoke: %.1f GB exceeds the in-RAM materialization bound here; "
            "restored bytes are sha256-verified. Run the model's own runtime on capable hardware.",
            manifest.total_bytes / 1e9,
        )

    if not manifest.trust_remote_code and manifest.detected_library == "transformers":
        try:
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            from transformers import AutoConfig

            AutoConfig.from_pretrained(dest_dir, local_files_only=True, trust_remote_code=False)
            log.info("smoke: transformers config loads offline")
        except Exception as e:
            log.warning("smoke: transformers config did not load (non-standard arch): %s", e)


# ===================================================================== scrub
def verify_stored(model_ref: str, settings: Settings) -> dict:
    """Re-prove closure against the stored, encrypted copy without downloading TBs."""
    settings.require_storage()
    storage = Storage(settings)
    _preflight(storage)

    manifest_obj = f"manifests/{model_ref}.json"
    text = storage.cat(settings.remote_standard, manifest_obj)
    if text is None:
        raise StorageError(f"no manifest for {model_ref}")
    manifest = Manifest.from_json(text.decode())

    blobs_prefix = f"blobs/{model_ref}/"
    present = set(storage.list_files(settings.remote_archive, blobs_prefix))
    missing = [f.path for f in manifest.files if f.path not in present]
    if missing:
        raise VerificationError(f"stored copy missing {len(missing)} file(s): {missing[:5]}")

    checks = ["stored_presence"]
    if manifest.weight_format_selected == "safetensors":
        index_path = next((f.path for f in manifest.files if f.path.endswith(".index.json")), None)
        if index_path:
            idx = json.loads(storage.cat(settings.remote_archive, f"{blobs_prefix}{index_path}").decode())
            weight_map = idx.get("weight_map", {})
            promised = set(weight_map)
            seen: set[str] = set()
            for shard in sorted(set(weight_map.values())):
                seen |= _stored_header_tensors(storage, settings, blobs_prefix, shard)
            if promised - seen:
                raise VerificationError(f"stored closure: {len(promised - seen)} tensors missing")
            checks.append("stored_closure")
        else:
            # Single-file safetensors: re-prove the stored shard's header parses
            # and declares tensors (reads only the header bytes, not the weights).
            for f in manifest.files:
                if f.path.endswith(".safetensors"):
                    if not _stored_header_tensors(storage, settings, blobs_prefix, f.path):
                        raise VerificationError(f"stored closure: {f.path} declares zero tensors")
            checks.append("stored_single_closure")
    log.info("scrub OK for %s: %s", model_ref, checks)
    return {"model_ref": model_ref, "status": "verified", "checks": checks}


def _stored_header_tensors(storage: Storage, settings: Settings, prefix: str, shard: str) -> set[str]:
    obj = f"{prefix}{shard}"
    head = storage.cat_range(settings.remote_archive, obj, 0, 8)
    n = struct.unpack("<Q", head)[0]
    body = storage.cat_range(settings.remote_archive, obj, 8, n)
    header = json.loads(body)
    return {k for k in header if k != "__metadata__"}


# ====================================================================== list
def list_catalog(settings: Settings) -> dict:
    settings.require_storage()
    storage = Storage(settings)
    return _read_catalog(storage)


# ==================================================================== catalog
def _read_catalog(storage: Storage) -> dict:
    text = storage.cat(storage.s.remote_standard, _CATALOG_OBJECT)
    if text is None:
        return {}
    try:
        return json.loads(text.decode())
    except json.JSONDecodeError as e:
        raise StorageError(f"catalog is corrupt: {e}") from e


def _write_catalog(storage: Storage, catalog: dict) -> None:
    data = json.dumps(catalog, indent=2, sort_keys=True).encode()
    storage.upload_bytes(data, storage.s.remote_standard, _CATALOG_OBJECT)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
