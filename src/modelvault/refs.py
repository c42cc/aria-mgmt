"""Resolve a user-supplied URL/shorthand into a stable, deterministic identity.

`model_ref` is the canonical id the whole system keys on. It is deterministic
(`repo@sha`), so re-running `backup` targets the same objects and idempotency is
free. The resolved 40-char commit SHA is what makes a backup reproducible and
identity-stable *after the source dies*.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from . import SourceError

_HF_HOST = "huggingface.co"
# org/model — HF ids are restricted to these characters.
_HF_SHORTHAND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class ParsedRef:
    source_type: str  # "hf" | "http" | "git"
    repo_id: str  # org/model for hf; the URL for http/git
    revision_requested: str  # branch/tag/sha as asked, or "main"
    source_url: str


@dataclass(frozen=True)
class ModelRef:
    source_type: str
    repo_id: str
    revision_requested: str
    revision_resolved_sha: str
    source_url: str

    @property
    def model_ref(self) -> str:
        if self.source_type == "hf":
            return f"hf/{self.repo_id}@{self.revision_resolved_sha}"
        digest = hashlib.sha256(self.source_url.encode()).hexdigest()[:16]
        return f"{self.source_type}/{digest}@{self.revision_resolved_sha}"

    @property
    def blobs_prefix(self) -> str:
        return f"blobs/{self.model_ref}/"

    @property
    def manifest_object(self) -> str:
        return f"manifests/{self.model_ref}.json"


def parse(url_or_shorthand: str, *, source_type: str = "auto", revision: str | None = None) -> ParsedRef:
    """Parse input into (source_type, repo_id, revision_requested) — no network."""
    raw = url_or_shorthand.strip()
    if not raw:
        raise SourceError("empty model URL/shorthand")

    rev = revision

    if source_type in ("auto", "hf"):
        # Bare org/model shorthand.
        if _HF_SHORTHAND.match(raw) and "://" not in raw:
            return ParsedRef("hf", raw, rev or "main", f"https://{_HF_HOST}/{raw}")
        parsed = urlparse(raw)
        if parsed.netloc == _HF_HOST or parsed.netloc.endswith("." + _HF_HOST):
            repo_id, url_rev = _parse_hf_path(parsed.path)
            return ParsedRef("hf", repo_id, rev or url_rev or "main", f"https://{_HF_HOST}/{repo_id}")
        if source_type == "hf":
            raise SourceError(f"not a Hugging Face URL/shorthand: {raw!r}")

    if source_type == "git" or (source_type == "auto" and raw.endswith(".git")):
        return ParsedRef("git", raw, rev or "HEAD", raw)

    if source_type in ("auto", "http"):
        parsed = urlparse(raw)
        if parsed.scheme in ("http", "https"):
            return ParsedRef("http", raw, rev or "", raw)

    raise SourceError(
        f"could not determine source type for {raw!r}",
        fix="pass --source-type hf|http|git explicitly",
    )


def _parse_hf_path(path: str) -> tuple[str, str | None]:
    """`/org/model[/tree/<ref>]` -> (org/model, ref|None)."""
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise SourceError(f"Hugging Face URL is missing org/model: {path!r}")
    repo_id = f"{parts[0]}/{parts[1]}"
    rev = None
    if len(parts) >= 4 and parts[2] in ("tree", "blob", "resolve"):
        rev = parts[3]
    return repo_id, rev
