"""The manifest — the one primitive everything else is a pure function of.

A manifest fully describes one backup: identity, pinned revision, every file with
its size and SHA-256, the verbatim small config files (so the model is
self-describing without the source or the remote), and the verification record.

The catalog (`index/index.json`) is a thin projection of manifests that `list`
reads. The `verified` flag in the catalog is the single trust bit; it flips
*last*, so a crash never leaves a "done" lie.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from . import SCHEMA_VERSION


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class FileEntry:
    path: str
    size: int
    sha256: str | None  # HF LFS sha256 for weights; computed locally for small files
    lfs: bool


@dataclass
class Verification:
    # completeness gate (the trust gate) — provable at any scale, no code exec
    status: str = "incomplete"  # "verified" only when the completeness gate passes
    completeness_checks: list[str] = field(default_factory=list)
    # loadability tier — recorded, NOT a fallback for the gate
    loadability_level: str = "none"  # skeleton_ok | structural_only | deferred | none
    loadability_method: str = ""  # e.g. transformers-skeleton, ast-structural
    loadability_executed_repo_code: bool = False
    verified_at: str = ""
    tool_versions: dict[str, str] = field(default_factory=dict)


@dataclass
class Storage:
    remote_archive: str = ""
    remote_standard: str = ""
    blobs_prefix: str = ""
    manifest_object: str = ""
    blob_storage_class: str = "ARCHIVE"


@dataclass
class Manifest:
    model_ref: str
    source_url: str
    source_type: str
    repo_id: str
    revision_requested: str
    revision_resolved_sha: str
    created_at: str
    detected_library: str
    weight_format_selected: str
    trust_remote_code: bool
    total_bytes: int
    files: list[FileEntry]
    embedded_identity: dict[str, str | None]
    verification: Verification
    storage: Storage
    schema_version: int = SCHEMA_VERSION
    encryption: dict[str, str] = field(
        default_factory=lambda: {"tool": "rclone-crypt", "filename_encryption": "standard"}
    )

    def to_json(self) -> str:
        return json.dumps(_to_dict(self), indent=2, sort_keys=False)

    @staticmethod
    def from_json(text: str) -> "Manifest":
        d = json.loads(text)
        return Manifest(
            model_ref=d["model_ref"],
            source_url=d["source_url"],
            source_type=d["source_type"],
            repo_id=d["repo_id"],
            revision_requested=d["revision_requested"],
            revision_resolved_sha=d["revision_resolved_sha"],
            created_at=d["created_at"],
            detected_library=d["detected_library"],
            weight_format_selected=d["weight_format_selected"],
            trust_remote_code=d["trust_remote_code"],
            total_bytes=d["total_bytes"],
            files=[FileEntry(**f) for f in d["files"]],
            embedded_identity=d["embedded_identity"],
            verification=Verification(**d["verification"]),
            storage=Storage(**d["storage"]),
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            encryption=d.get("encryption", {}),
        )

    def catalog_entry(self) -> dict:
        return {
            "repo_id": self.repo_id,
            "revision_resolved_sha": self.revision_resolved_sha,
            "source_type": self.source_type,
            "total_bytes": self.total_bytes,
            "created_at": self.created_at,
            "status": self.verification.status,
            "trust_remote_code": self.trust_remote_code,
            "loadability_level": self.verification.loadability_level,
        }


def _to_dict(m: Manifest) -> dict:
    d = asdict(m)
    # Stable, human-first key order in the serialized file.
    order = [
        "schema_version", "model_ref", "source_url", "source_type", "repo_id",
        "revision_requested", "revision_resolved_sha", "created_at",
        "detected_library", "weight_format_selected", "trust_remote_code",
        "total_bytes", "files", "embedded_identity", "verification",
        "encryption", "storage",
    ]
    return {k: d[k] for k in order if k in d}
