"""Environment-driven configuration for ModelVault.

Same pattern as `src/config.py`: load the repo `.env` once, read `os.getenv`
into a frozen dataclass. ModelVault is its own concern, so it keeps its own
settings object rather than bloating the voice-bot Config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from . import ConfigError

load_dotenv()

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    # rclone is the one external binary and the byte path.
    rclone_bin: str = os.getenv("MODELVAULT_RCLONE_BIN", "rclone")
    # Path to an (encrypted) rclone.conf holding the crypt remotes. When unset,
    # rclone uses its own default config location.
    rclone_conf: str = os.getenv("MODELVAULT_RCLONE_CONF", "")
    # Password that decrypts rclone.conf at runtime (rclone's RCLONE_CONFIG_PASS).
    # The crown jewel. Never committed, never placed in the remote.
    rclone_config_pass: str = os.getenv("RCLONE_CONFIG_PASS", "")

    # Two crypt remotes, one key. ARCHIVE for blobs, STANDARD for catalog+manifests.
    remote_archive: str = os.getenv("MODELVAULT_REMOTE_ARCHIVE", "vault-archive")
    remote_standard: str = os.getenv("MODELVAULT_REMOTE_STANDARD", "vault-standard")

    # GCS identity used by the provisioning script (not by the running tool — the
    # tool only ever addresses rclone remotes).
    gcp_project: str = os.getenv("MODELVAULT_GCP_PROJECT", "")
    bucket: str = os.getenv("MODELVAULT_BUCKET", "")
    sa_key_file: str = os.getenv("MODELVAULT_SA_KEY_FILE", "")

    # Small-files temp dir. Holds config/tokenizer/header bytes only — never weights.
    tmp_dir: str = os.getenv(
        "MODELVAULT_TMP_DIR", str(_REPO_ROOT / "data" / "modelvault" / "tmp")
    )

    # Hugging Face auth for gated/private repos (optional for public models).
    hf_token: str = os.getenv("HF_TOKEN", "") or os.getenv("HUGGING_FACE_HUB_TOKEN", "")
    hf_endpoint: str = os.getenv("HF_ENDPOINT", "https://huggingface.co")

    # A file is "small" (fetched whole into tmp for identity/verify) below this.
    # Everything larger is a weight blob that only ever streams through rclone.
    # Kimi's 23 MB index and 2.8 MB tiktoken model sit comfortably under this.
    small_file_max_bytes: int = int(
        os.getenv("MODELVAULT_SMALL_FILE_MAX_BYTES", str(64 * 1024 * 1024))
    )

    # Parallel rclone streams for the blob phase.
    transfers: int = int(os.getenv("MODELVAULT_TRANSFERS", "4"))

    @property
    def repo_root(self) -> Path:
        return _REPO_ROOT

    def require_storage(self) -> None:
        """Fail loudly if the configured rclone.conf path does not exist."""
        if self.rclone_conf and not os.path.exists(self.rclone_conf):
            raise ConfigError(
                f"MODELVAULT_RCLONE_CONF points at a missing file: {self.rclone_conf}",
                fix="bash ops/modelvault_provision.sh   # creates the crypt remotes",
            )


def load_settings() -> Settings:
    return Settings()
