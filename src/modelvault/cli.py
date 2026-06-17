"""modelvault CLI.

  modelvault backup  <URL> [--revision REF] [--source-type auto|hf|http|git] [--force]
  modelvault restore <MODEL_REF> [--dest DIR] [--smoke-test]
  modelvault verify  <MODEL_REF>
  modelvault list    [--json]
  modelvault doctor

Exit codes (the contract): 0 ok, 2 verification failed, 3 source unreachable,
4 storage/auth error. All operations log structured JSON to stderr.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

import typer

from . import EXIT_OK, ModelVaultError
from .config import load_settings

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Verified, encrypted cold backups of (TB-scale) models.")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger("modelvault")
    root.handlers[:] = [handler]
    root.setLevel(os.getenv("MODELVAULT_LOG_LEVEL", "INFO").upper())
    root.propagate = False


def _guard(fn, *args, **kwargs):
    """Run a pipeline call; map our errors to the exit-code contract, loudly."""
    try:
        return fn(*args, **kwargs)
    except ModelVaultError as e:
        log = logging.getLogger("modelvault.cli")
        log.error("%s: %s", type(e).__name__, e)
        if e.fix:
            log.error("fix: %s", e.fix)
        raise typer.Exit(code=e.exit_code)


@app.command()
def backup(
    url: str = typer.Argument(..., help="Model URL or org/model shorthand"),
    revision: str = typer.Option(None, "--revision", help="Branch/tag/sha (default: main)"),
    source_type: str = typer.Option("auto", "--source-type", help="auto|hf|http|git"),
    force: bool = typer.Option(False, "--force", help="Re-run even if already verified"),
):
    from . import pipeline

    _setup_logging()
    settings = load_settings()
    manifest = _guard(pipeline.backup, url, settings, revision=revision, source_type=source_type, force=force)
    typer.echo(
        f"verified: {manifest.model_ref}\n"
        f"  files={len(manifest.files)} bytes={manifest.total_bytes} "
        f"loadability={manifest.verification.loadability_level}\n"
        f"  blobs={manifest.storage.remote_archive}:{manifest.storage.blobs_prefix}"
    )


@app.command()
def restore(
    model_ref: str = typer.Argument(..., help="The MODEL_REF to restore"),
    dest: str = typer.Option(None, "--dest", help="Destination dir (default ./restored/<ref>)"),
    smoke_test: bool = typer.Option(False, "--smoke-test", help="Load offline after download"),
):
    from . import pipeline

    _setup_logging()
    settings = load_settings()
    path = _guard(pipeline.restore, model_ref, settings, dest=dest, smoke=smoke_test)
    typer.echo(path)


@app.command()
def verify(model_ref: str = typer.Argument(..., help="The MODEL_REF to scrub")):
    from . import pipeline

    _setup_logging()
    settings = load_settings()
    result = _guard(pipeline.verify_stored, model_ref, settings)
    typer.echo(json.dumps(result))


@app.command(name="list")
def list_cmd(json_out: bool = typer.Option(False, "--json", help="Emit raw catalog JSON")):
    from . import pipeline

    _setup_logging()
    settings = load_settings()
    catalog = _guard(pipeline.list_catalog, settings)
    if json_out:
        typer.echo(json.dumps(catalog, indent=2, sort_keys=True))
        return
    if not catalog:
        typer.echo("(vault empty)")
        return
    for ref, e in sorted(catalog.items()):
        typer.echo(
            f"{e.get('status','?'):9s} {round(e.get('total_bytes',0)/1e9,2):>8} GB  "
            f"trc={int(bool(e.get('trust_remote_code')))} "
            f"load={e.get('loadability_level','?'):16s} {ref}"
        )


@app.command()
def doctor():
    from .storage import Storage

    _setup_logging()
    settings = load_settings()
    probes = Storage(settings).doctor()
    ok = True
    for p in probes:
        if p.ok:
            typer.echo(f"OK   {p.detail}")
        else:
            ok = False
            typer.echo(f"FAIL {p.error}\n     fix: {p.fix}\n     {p.detail}")
    raise typer.Exit(code=EXIT_OK if ok else 4)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
