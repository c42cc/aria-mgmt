"""Acquire — the Hugging Face source. Diskless probe + URL resolution.

This stage never downloads weights. It:
  1. resolves the requested revision to a pinned 40-char commit SHA,
  2. enumerates every file with size + authoritative LFS sha256,
  3. downloads only the *small* files (config/tokenizer/index/code) to a temp dir,
  4. range-reads each safetensors header (8-byte length prefix + JSON) — KBs,
  5. resolves each file's final, fetchable URL for rclone to stream.

Everything here is read-only against the source and bounded in size regardless of
how many terabytes the model is.
"""

from __future__ import annotations

import json
import logging
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

import requests
from huggingface_hub import HfApi, hf_hub_download, hf_hub_url
from huggingface_hub import get_hf_file_metadata
from huggingface_hub.utils import GatedRepoError, RepositoryNotFoundError, RevisionNotFoundError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from . import SourceError
from .config import Settings
from .manifest import FileEntry
from .refs import ModelRef, ParsedRef

log = logging.getLogger("modelvault.acquire")

_WEIGHT_EXT = {".safetensors", ".bin", ".gguf", ".pt", ".pth", ".msgpack", ".h5", ".onnx", ".ckpt"}
_IDENTITY_FILES = ("config.json", "tokenizer_config.json", "generation_config.json")


@dataclass
class FileLoc:
    url: str
    size: int
    etag: str


@dataclass
class AcquireProbe:
    mref: ModelRef
    files: list[FileEntry]
    total_bytes: int
    tmp_dir: str
    small_files: dict[str, str]  # logical path -> local path
    headers: dict[str, dict]  # shard path -> parsed safetensors header
    index: dict | None
    config: dict
    embedded_identity: dict[str, str | None]
    auto_map: dict | None
    trust_remote_code: bool
    weight_format: str
    detected_library: str
    py_files: list[str] = field(default_factory=list)


def resolve(parsed: ParsedRef, settings: Settings) -> ModelRef:
    if parsed.source_type != "hf":
        raise SourceError(
            f"source_type {parsed.source_type!r} is not implemented yet (M5: generic http/git)",
            fix="back up a Hugging Face model for now",
        )
    api = HfApi(token=settings.hf_token or None, endpoint=settings.hf_endpoint)
    try:
        info = api.repo_info(parsed.repo_id, revision=parsed.revision_requested, files_metadata=False)
    except (RepositoryNotFoundError, RevisionNotFoundError, GatedRepoError) as e:
        raise SourceError(
            f"cannot resolve {parsed.repo_id}@{parsed.revision_requested}: {type(e).__name__}",
            fix="check the URL, or set HF_TOKEN for a gated/private repo",
        ) from e
    except Exception as e:  # network/transport — our problem to surface, not hide
        raise SourceError(f"Hugging Face repo_info failed for {parsed.repo_id}: {e}") from e
    if not info.sha:
        raise SourceError(f"Hugging Face returned no commit SHA for {parsed.repo_id}")
    return ModelRef(
        source_type="hf",
        repo_id=parsed.repo_id,
        revision_requested=parsed.revision_requested,
        revision_resolved_sha=info.sha,
        source_url=parsed.source_url,
    )


def probe(mref: ModelRef, settings: Settings) -> AcquireProbe:
    api = HfApi(token=settings.hf_token or None, endpoint=settings.hf_endpoint)
    try:
        info = api.repo_info(mref.repo_id, revision=mref.revision_resolved_sha, files_metadata=True)
    except Exception as e:
        raise SourceError(f"Hugging Face file metadata failed for {mref.repo_id}: {e}") from e

    siblings = info.siblings or []
    if not siblings:
        raise SourceError(f"{mref.repo_id}@{mref.revision_resolved_sha} reports zero files")

    files: list[FileEntry] = []
    for s in siblings:
        lfs = getattr(s, "lfs", None)
        lfs_sha = None
        if isinstance(lfs, dict):
            lfs_sha = lfs.get("sha256")
        elif lfs is not None:
            lfs_sha = getattr(lfs, "sha256", None)
        files.append(
            FileEntry(path=s.rfilename, size=int(s.size or 0), sha256=lfs_sha, lfs=lfs is not None)
        )

    total_bytes = sum(f.size for f in files)
    tmp_dir = str(Path(settings.tmp_dir) / mref.model_ref)
    os.makedirs(tmp_dir, exist_ok=True)

    # Download only small files (identity/config/index/code), never weights.
    small_files: dict[str, str] = {}
    py_files: list[str] = []
    for f in files:
        ext = Path(f.path).suffix.lower()
        if ext in _WEIGHT_EXT:
            continue
        if f.size > settings.small_file_max_bytes:
            continue
        local = _download_small(mref, f.path, settings, tmp_dir)
        small_files[f.path] = local
        if ext == ".py":
            py_files.append(f.path)

    # Compute sha256 for small non-LFS files (we hold them; close the trust gap).
    import hashlib

    for f in files:
        if f.sha256 is None and f.path in small_files:
            f.sha256 = _sha256_file(small_files[f.path], hashlib)

    config = _read_json(small_files.get("config.json"))
    auto_map = config.get("auto_map") if isinstance(config, dict) else None
    trust_remote_code = bool(auto_map)

    embedded_identity = {name: _read_text(small_files.get(name)) for name in _IDENTITY_FILES}

    index = None
    index_path = next((f.path for f in files if f.path.endswith(".safetensors.index.json")), None)
    if index_path and index_path in small_files:
        index = _read_json(small_files[index_path])

    weight_format = _detect_weight_format(files)
    detected_library = "transformers" if "config.json" in small_files else "unknown"

    headers: dict[str, dict] = {}
    if weight_format == "safetensors":
        for f in files:
            if f.path.endswith(".safetensors"):
                headers[f.path] = read_safetensors_header(mref, f.path, f.size, settings)

    return AcquireProbe(
        mref=mref,
        files=files,
        total_bytes=total_bytes,
        tmp_dir=tmp_dir,
        small_files=small_files,
        headers=headers,
        index=index,
        config=config if isinstance(config, dict) else {},
        embedded_identity=embedded_identity,
        auto_map=auto_map,
        trust_remote_code=trust_remote_code,
        weight_format=weight_format,
        detected_library=detected_library,
        py_files=py_files,
    )


@retry(
    retry=retry_if_exception_type(SourceError),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    reraise=True,
)
def file_location(mref: ModelRef, path: str, settings: Settings) -> FileLoc:
    """Resolve a file's final fetchable URL (follows HF's redirect to storage)."""
    url = hf_hub_url(mref.repo_id, path, revision=mref.revision_resolved_sha, endpoint=settings.hf_endpoint)
    try:
        meta = get_hf_file_metadata(url, token=settings.hf_token or None)
    except Exception as e:
        raise SourceError(f"could not resolve URL for {path}: {e}") from e
    return FileLoc(url=meta.location or url, size=int(meta.size or 0), etag=meta.etag or "")


def read_safetensors_header(mref: ModelRef, path: str, size: int, settings: Settings) -> dict:
    loc = file_location(mref, path, settings)
    prefix = _range_get(loc.url, 0, 8, settings)
    if len(prefix) != 8:
        raise SourceError(f"{path}: could not read 8-byte safetensors length prefix")
    n = struct.unpack("<Q", prefix)[0]
    if n <= 0 or n > size:
        raise SourceError(f"{path}: implausible safetensors header length {n} (file size {size})")
    body = _range_get(loc.url, 8, n, settings)
    if len(body) != n:
        raise SourceError(f"{path}: short safetensors header read ({len(body)} != {n})")
    try:
        header = json.loads(body)
    except json.JSONDecodeError as e:
        raise SourceError(f"{path}: safetensors header is not valid JSON: {e}") from e
    return {"header": header, "header_size": n, "file_size": size}


# ---------------------------------------------------------------------- helpers
@retry(
    retry=retry_if_exception_type(SourceError),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    reraise=True,
)
def _range_get(url: str, offset: int, count: int, settings: Settings) -> bytes:
    headers = {"Range": f"bytes={offset}-{offset + count - 1}"}
    if settings.hf_token:
        headers["Authorization"] = f"Bearer {settings.hf_token}"
    try:
        r = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
    except requests.RequestException as e:
        raise SourceError(f"range read failed: {e}") from e
    if r.status_code not in (200, 206):
        raise SourceError(f"range read HTTP {r.status_code}")
    return r.content


@retry(
    retry=retry_if_exception_type(SourceError),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    reraise=True,
)
def _download_small(mref: ModelRef, path: str, settings: Settings, tmp_dir: str) -> str:
    try:
        return hf_hub_download(
            mref.repo_id,
            path,
            revision=mref.revision_resolved_sha,
            local_dir=tmp_dir,
            token=settings.hf_token or None,
            endpoint=settings.hf_endpoint,
        )
    except Exception as e:
        raise SourceError(f"could not download small file {path}: {e}") from e


def _detect_weight_format(files: list[FileEntry]) -> str:
    names = [f.path.lower() for f in files]
    if any(n.endswith(".safetensors") for n in names):
        return "safetensors"
    if any(n.endswith(".gguf") for n in names):
        return "gguf"
    if any(n.endswith((".bin", ".pt", ".pth")) for n in names):
        return "pytorch"
    return "unknown"


def _read_text(path: str | None) -> str | None:
    if not path or not os.path.exists(path):
        return None
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _read_json(path: str | None):
    text = _read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _sha256_file(path: str, hashlib_mod) -> str:
    h = hashlib_mod.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
