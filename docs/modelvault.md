# ModelVault — verified, encrypted cold backups of (TB-scale) models

`modelvault` turns a model URL into a **verified, client-side-encrypted, self-describing**
cold backup in an `rclone` remote (GCS Archive by default) that stays usable after
the source link dies, and restores to a directory that loads offline. It is a
standalone CLI under `src/modelvault/` — it shares no runtime with the voice bot.

It is built for the primary reality that these models are **terabytes** (Kimi-K2.6
is ~0.6 TB on disk, 64 shards, custom code), larger than local disk. So nothing is
ever staged whole on disk: `rclone` streams each file `source -> encrypt -> remote`
and verification runs from KB-sized header reads.

## The one idea

Everything is a pure function of one primitive: a **manifest keyed by
`model_ref = hf/<org>/<model>@<sha>`**, with a single `verified` flag that only the
completeness gate can set, flipped into the catalog **last**.

```
backup:  resolve -> probe (small files + range-read headers) -> VERIFY (gate)
         -> stream blobs (rclone copyurl, crypt) -> write manifest -> flip catalog LAST
restore: pull manifest -> rclone copy + decrypt -> re-verify every sha256 -> [smoke]
verify:  re-prove closure against the stored, encrypted copy (scrub)
list:    read the catalog, nothing else
```

## Verification: a gate and a recorded tier (never conflated)

**Completeness gate** (the trust bit; provable at any scale; **never runs repo code**):
- `manifest_closure` — every tensor the safetensors index promises is present in
  exactly one shard header; no missing, no extra. Proven from 8-byte length-prefix
  + JSON header range-reads (KBs), not by downloading weights.
- `identity_present` — `config.json` captured (the model is self-describing).
- `tokenizer_consistency` — a declared tokenizer has its data files captured.
- `code_capture` — every `auto_map`-referenced `.py` is captured and the named
  class actually exists in it (checked by **AST parse, not by importing it**).

If the gate fails the artifact is **not** stored as trusted (exit 2).

**Loadability tier** (recorded in the manifest, *not* a fallback for the gate):
- standard archs: skeleton key-match on the `meta` device (zero weight memory —
  feasible even at 1T params);
- custom-code models (e.g. Kimi): a structural AST check; **their Python is not run**.

The authoritative "it loads" oracle is `restore --smoke-test` on hardware that fits
the bytes — never this Mac, never silent code-exec. This is why `trust_remote_code`
models are backed up safely without a Docker sandbox.

> Env note: the skeleton path needs `torch >= 2.4`. This repo pins `torch 2.2.2`
> (numpy<2 ABI), so on this machine the skeleton tier records `skeleton_error`
> honestly — the completeness gate is unaffected, and our two real targets
> (PP-OCRv6, non-standard; Kimi, custom-code) never use the skeleton path anyway.

## Confidentiality (what the provider can and cannot see)

Two crypt remotes (ARCHIVE for blobs, STANDARD for catalog + manifests) share **one**
password+salt held only in a local mode-600 `rclone.conf`. Everything — including the
catalog — is encrypted client-side before it reaches the provider. Verified cold:
object names are obscured and bytes begin with the `RCLONE\x00\x00` crypt header; the
plaintext `model_type` appears nowhere in the store. The provider can still infer
coarse metadata (object **sizes, counts, timing**) — not contents or names.

## Setup

```bash
# Local / removable target (also the test substrate, the spec's "offline target"):
bash ops/modelvault_provision.sh --local /Volumes/BigDisk/vault --conf ~/.config/modelvault/rclone.conf

# GCS (dedicated bucket + service account + versioning, one crypt key):
gcloud auth login                       # once, if your token is stale
bash ops/modelvault_provision.sh        # resolves the project nearest "agi_env_general"
```

The script prints the exact `.env` lines to add (it never writes secrets for you).
`MODELVAULT_RCLONE_CONF` is the crown jewel — keep an **offline** backup of it; losing
it loses the data irrecoverably.

## Usage

```bash
modelvault doctor                                   # rclone + remotes reachable?
modelvault backup https://huggingface.co/moonshotai/Kimi-K2.6
modelvault list
modelvault restore hf/moonshotai/Kimi-K2.6@<sha> --dest /Volumes/BigDisk/kimi --smoke-test
modelvault verify hf/moonshotai/Kimi-K2.6@<sha>     # scrub the stored copy
```

Exit codes: `0` ok, `2` verification failed (not stored/trusted), `3` source
unreachable, `4` storage/auth error. All operations log structured JSON to stderr.

## Recovery runbook (must work from nothing but the key + the bucket)

A custody system never restored from is theater. On a clean machine:

1. `brew install rclone` and install the Python deps (`pip install -r requirements.txt`).
2. Restore the mode-600 `rclone.conf` (your offline copy of the crypt key) and a
   read-capable service-account key; set `MODELVAULT_RCLONE_CONF` (and, for GCS,
   `MODELVAULT_SA_KEY_FILE`) in `.env`.
3. `modelvault list` — confirm the catalog decrypts and shows the backup.
4. `modelvault restore <MODEL_REF> --dest <big-volume> --smoke-test`.

If step 4 succeeds with Hugging Face fully blocked, the backup is real and
source-independent.

## Acceptance tests (spec §10)

Deterministic negatives + an end-to-end round-trip live in
[`tests/test_modelvault.py`](../tests/test_modelvault.py):

```bash
.venv/bin/python -m unittest tests.test_modelvault -v                 # offline unit (gate negatives)
MODELVAULT_E2E=1 .venv/bin/python -m unittest tests.test_modelvault -v # stream+encrypt+restore+idempotent
```

The unit tests prove the verifier **FAILS** on a missing shard, a missing tensor, an
extra tensor, a missing tokenizer file, missing/incorrect custom code, and a restored
sha256 mismatch. The E2E test streams the real tiny model through crypt to a local
remote, proves ciphertext at rest (no plaintext leak), restores with sha256
re-verification, and proves idempotency (a second backup is a no-op).
