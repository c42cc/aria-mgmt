"""The rclone wrapper — the one external binary, and the entire byte path.

Every remote interaction goes through here. `rclone` owns streaming, encryption
(crypt remote), checksums, parallelism, and resume. We never move model bytes
through Python; we hand rclone a source URL and a crypt destination and let it
stream. Failures are surfaced loudly with rclone's stderr attached — never
swallowed, never blamed on the remote.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from . import StorageError
from .config import Settings

log = logging.getLogger("modelvault.storage")


@dataclass
class Probe:
    ok: bool
    error: str
    fix: str
    detail: str


class Storage:
    def __init__(self, settings: Settings):
        self.s = settings

    # ------------------------------------------------------------------ rclone
    def _base(self) -> list[str]:
        binary = shutil.which(self.s.rclone_bin) or self.s.rclone_bin
        argv = [binary]
        if self.s.rclone_conf:
            argv += ["--config", self.s.rclone_conf]
        return argv

    def _env(self) -> dict[str, str]:
        import os

        env = dict(os.environ)
        if self.s.rclone_config_pass:
            env["RCLONE_CONFIG_PASS"] = self.s.rclone_config_pass
        return env

    def _run(self, args: list[str], *, stdin: bytes | None = None) -> subprocess.CompletedProcess:
        argv = self._base() + args
        log.debug("rclone %s", " ".join(args[:2]))
        try:
            proc = subprocess.run(
                argv,
                input=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env(),
                check=False,
            )
        except FileNotFoundError as e:
            raise StorageError(
                f"rclone binary not found ({self.s.rclone_bin!r})",
                fix="brew install rclone  (or set MODELVAULT_RCLONE_BIN)",
            ) from e
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace").strip()
            raise StorageError(
                f"rclone {args[0]} failed (exit {proc.returncode}): {stderr[-800:]}"
            )
        return proc

    @retry(
        retry=retry_if_exception_type(StorageError),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def _run_retrying(self, args: list[str], *, stdin: bytes | None = None) -> subprocess.CompletedProcess:
        return self._run(args, stdin=stdin)

    # --------------------------------------------------------------- streaming
    def stream_url(self, url: str, remote: str, path: str) -> None:
        """Stream a source URL straight into an encrypted object (no local copy)."""
        self._run_retrying(["copyurl", url, f"{remote}:{path}", "--transfers", "1"])

    def upload_bytes(self, data: bytes, remote: str, path: str) -> None:
        self._run_retrying(["rcat", f"{remote}:{path}"], stdin=data)

    def download_dir(self, remote: str, prefix: str, local_dir: str) -> None:
        self._run_retrying(
            ["copy", f"{remote}:{prefix}", local_dir, "--checksum", "--transfers", str(self.s.transfers)]
        )

    # ----------------------------------------------------------------- reading
    def list_files(self, remote: str, prefix: str) -> list[str]:
        """Recursive logical (decrypted) file paths under prefix; [] if absent."""
        try:
            proc = self._run(["lsf", "-R", "--files-only", f"{remote}:{prefix}"])
        except StorageError as e:
            if "directory not found" in str(e).lower() or "not found" in str(e).lower():
                return []
            raise
        return [ln for ln in proc.stdout.decode().splitlines() if ln]

    def exists(self, remote: str, path: str) -> bool:
        directory, _, name = path.rpartition("/")
        return name in {p.rstrip("/") for p in self.list_files(remote, directory)}

    def cat(self, remote: str, path: str) -> bytes | None:
        if not self.exists(remote, path):
            return None
        return self._run_retrying(["cat", f"{remote}:{path}"]).stdout

    def cat_range(self, remote: str, path: str, offset: int, count: int) -> bytes:
        return self._run_retrying(
            ["cat", f"{remote}:{path}", "--offset", str(offset), "--count", str(count)]
        ).stdout

    # ------------------------------------------------------------------ doctor
    def doctor(self) -> list[Probe]:
        probes: list[Probe] = []
        binary = shutil.which(self.s.rclone_bin)
        if not binary:
            probes.append(Probe(False, "rclone not installed", "brew install rclone", self.s.rclone_bin))
            return probes
        probes.append(Probe(True, "", "", binary))

        try:
            out = self._run(["listremotes"]).stdout.decode()
        except StorageError as e:
            fix = "rclone config" if "RCLONE_CONFIG_PASS" not in str(e) else "set RCLONE_CONFIG_PASS"
            probes.append(Probe(False, "cannot read rclone config", fix, str(e)))
            return probes

        remotes = {r.rstrip(":") for r in out.split()}
        for name in (self.s.remote_archive, self.s.remote_standard):
            if name in remotes:
                probes.append(Probe(True, "", "", f"remote {name} present"))
            else:
                probes.append(
                    Probe(
                        False,
                        f"rclone remote {name!r} missing",
                        "bash ops/modelvault_provision.sh",
                        f"have: {sorted(remotes)}",
                    )
                )
        return probes
