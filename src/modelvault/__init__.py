"""ModelVault — verified, client-side-encrypted cold backups of (terabyte-scale) models.

One CLI: a model URL becomes a self-describing, encrypted backup in an `rclone`
remote (GCS Archive by default) that stays usable after the source dies, and
restores to a directory that loads offline.

Design invariants (do not weaken):
- Diskless. Weights never land on local disk; `rclone` streams source -> encrypt
  -> remote. The only local footprint is a small temp dir of config/header bytes.
- The byte path belongs to `rclone`, not Python. At terabyte scale, piping bytes
  through a Python hash loop is a throughput anti-pattern.
- Verification is two separable things: a *completeness gate* (provable from
  KB-sized header range-reads at any scale, never executes repo code) and a
  *recorded loadability tier*. The authoritative loader oracle runs at restore on
  hardware that fits the bytes.
- No silent failures, no fallbacks. Every stage raises loudly with a fix.
"""

from __future__ import annotations

__all__ = [
    "SCHEMA_VERSION",
    "EXIT_OK",
    "EXIT_VERIFY",
    "EXIT_SOURCE",
    "EXIT_STORAGE",
    "ModelVaultError",
    "SourceError",
    "VerificationError",
    "StorageError",
    "ConfigError",
]

SCHEMA_VERSION = 1

# Exit codes are the CLI contract (spec §4). They are part of the interface;
# callers/tests depend on them.
EXIT_OK = 0
EXIT_VERIFY = 2  # verification failed -> artifact NOT stored / NOT trusted
EXIT_SOURCE = 3  # source unreachable / source-side problem (still our fault)
EXIT_STORAGE = 4  # storage / auth / rclone error


class ModelVaultError(Exception):
    """Base error. Carries an optional one-line fix command for the operator."""

    exit_code = 1

    def __init__(self, message: str, *, fix: str | None = None):
        super().__init__(message)
        self.fix = fix


class ConfigError(ModelVaultError):
    exit_code = EXIT_STORAGE


class SourceError(ModelVaultError):
    exit_code = EXIT_SOURCE


class VerificationError(ModelVaultError):
    exit_code = EXIT_VERIFY


class StorageError(ModelVaultError):
    exit_code = EXIT_STORAGE
