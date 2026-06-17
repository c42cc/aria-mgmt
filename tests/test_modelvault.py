"""ModelVault tests.

Two layers:
  - Deterministic, offline unit tests for the completeness GATE. These encode the
    spec's "the verifier must FAIL" negatives (§10.3): a missing shard, a missing
    tokenizer file, missing custom code, and a restored-byte hash mismatch all
    raise. No network, no rclone.
  - An end-to-end test (set MODELVAULT_E2E=1) that provisions a local crypt vault,
    streams the real tiny model HF -> crypt -> local, proves ciphertext at rest,
    restores with sha256 re-verification, and proves idempotency. Needs network +
    rclone.

Run:  .venv/bin/python -m unittest tests.test_modelvault -v
E2E:  MODELVAULT_E2E=1 .venv/bin/python -m unittest tests.test_modelvault -v
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.modelvault import VerificationError, verify
from src.modelvault.acquire import AcquireProbe
from src.modelvault.manifest import FileEntry, Manifest, Verification, Storage as StorageBlock
from src.modelvault.refs import ModelRef, parse


def _mref() -> ModelRef:
    return ModelRef("hf", "org/model", "main", "0" * 40, "https://huggingface.co/org/model")


def _shard_probe(**over) -> AcquireProbe:
    """A healthy 2-shard safetensors probe; override fields to break it."""
    files = [
        FileEntry("model.safetensors.index.json", 100, "h", False),
        FileEntry("model-00001-of-00002.safetensors", 1000, "h1", True),
        FileEntry("model-00002-of-00002.safetensors", 1000, "h2", True),
        FileEntry("config.json", 50, "hc", False),
    ]
    headers = {
        "model-00001-of-00002.safetensors": {"header": {"w1": {}, "__metadata__": {}}},
        "model-00002-of-00002.safetensors": {"header": {"w2": {}}},
    }
    index = {"weight_map": {"w1": "model-00001-of-00002.safetensors", "w2": "model-00002-of-00002.safetensors"}}
    kw = dict(
        mref=_mref(),
        files=files,
        total_bytes=2150,
        tmp_dir="/tmp/none",
        small_files={},
        headers=headers,
        index=index,
        config={},
        embedded_identity={"config.json": "{}", "tokenizer_config.json": None, "generation_config.json": None},
        auto_map=None,
        trust_remote_code=False,
        weight_format="safetensors",
        detected_library="transformers",
        py_files=[],
    )
    kw.update(over)
    return AcquireProbe(**kw)


class TestRefs(unittest.TestCase):
    def test_hf_url(self):
        p = parse("https://huggingface.co/org/model")
        self.assertEqual((p.source_type, p.repo_id), ("hf", "org/model"))

    def test_hf_shorthand_and_tree(self):
        self.assertEqual(parse("org/model").repo_id, "org/model")
        self.assertEqual(parse("https://huggingface.co/org/model/tree/v2").revision_requested, "v2")

    def test_model_ref_format(self):
        m = ModelRef("hf", "org/model", "main", "a" * 40, "u")
        self.assertEqual(m.model_ref, f"hf/org/model@{'a'*40}")
        self.assertEqual(m.blobs_prefix, f"blobs/hf/org/model@{'a'*40}/")


class TestCompletenessGate(unittest.TestCase):
    def test_healthy_closure_passes(self):
        checks: list[str] = []
        verify._closure(_shard_probe(), checks)
        self.assertIn("manifest_closure", checks)

    def test_missing_shard_fails(self):
        probe = _shard_probe()
        probe.files = [f for f in probe.files if f.path != "model-00002-of-00002.safetensors"]
        with self.assertRaises(VerificationError):
            verify._closure(probe, [])

    def test_missing_tensor_fails(self):
        probe = _shard_probe(headers={
            "model-00001-of-00002.safetensors": {"header": {"w1": {}}},
            "model-00002-of-00002.safetensors": {"header": {}},  # w2 vanished
        })
        with self.assertRaises(VerificationError):
            verify._closure(probe, [])

    def test_extra_tensor_fails(self):
        probe = _shard_probe(headers={
            "model-00001-of-00002.safetensors": {"header": {"w1": {}, "w3": {}}},  # not in index
            "model-00002-of-00002.safetensors": {"header": {"w2": {}}},
        })
        with self.assertRaises(VerificationError):
            verify._closure(probe, [])

    def test_tokenizer_without_data_fails(self):
        probe = _shard_probe(embedded_identity={
            "config.json": "{}",
            "tokenizer_config.json": "{}",  # declares a tokenizer
            "generation_config.json": None,
        })
        with self.assertRaises(VerificationError):
            verify._tokenizer_consistency(probe, [])

    def test_tokenizer_with_data_passes(self):
        files = _shard_probe().files + [FileEntry("tokenizer.model", 10, "t", True)]
        probe = _shard_probe(files=files, embedded_identity={
            "config.json": "{}", "tokenizer_config.json": "{}", "generation_config.json": None,
        })
        checks: list[str] = []
        verify._tokenizer_consistency(probe, checks)
        self.assertIn("tokenizer_consistency", checks)

    def test_code_capture_missing_py_fails(self):
        probe = _shard_probe(auto_map={"AutoModel": "modeling_x.MyModel"}, trust_remote_code=True)
        with self.assertRaises(VerificationError):
            verify._code_capture(probe, [])

    def test_code_capture_with_py_passes(self):
        with tempfile.TemporaryDirectory() as d:
            py = Path(d) / "modeling_x.py"
            py.write_text("class MyModel:\n    pass\n")
            probe = _shard_probe(
                auto_map={"AutoModel": "modeling_x.MyModel"},
                trust_remote_code=True,
                small_files={"modeling_x.py": str(py)},
            )
            checks: list[str] = []
            verify._code_capture(probe, checks)
            self.assertIn("code_capture", checks)

    def test_code_capture_class_absent_fails(self):
        with tempfile.TemporaryDirectory() as d:
            py = Path(d) / "modeling_x.py"
            py.write_text("class Other:\n    pass\n")
            probe = _shard_probe(
                auto_map={"AutoModel": "modeling_x.MyModel"},
                trust_remote_code=True,
                small_files={"modeling_x.py": str(py)},
            )
            with self.assertRaises(VerificationError):
                verify._code_capture(probe, [])

    def test_custom_code_runs_structural_not_executed(self):
        with tempfile.TemporaryDirectory() as d:
            py = Path(d) / "modeling_x.py"
            py.write_text("class MyModel:\n    pass\n")
            probe = _shard_probe(
                auto_map={"AutoModel": "modeling_x.MyModel"},
                trust_remote_code=True,
                small_files={"modeling_x.py": str(py)},
            )
            from src.modelvault.config import load_settings

            v = verify.run_verification(probe, load_settings())
            self.assertEqual(v.status, "verified")
            self.assertEqual(v.loadability_level, "structural_only")
            self.assertFalse(v.loadability_executed_repo_code)


class TestRestoreIntegrity(unittest.TestCase):
    def test_hash_mismatch_fails(self):
        from src.modelvault import pipeline

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "w.safetensors").write_bytes(b"tampered")
            good = hashlib.sha256(b"original").hexdigest()
            manifest = _manifest_with([FileEntry("w.safetensors", 8, good, True)])
            with self.assertRaises(VerificationError):
                pipeline._reverify_hashes(manifest, d)

    def test_hash_match_passes(self):
        from src.modelvault import pipeline

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "w.safetensors").write_bytes(b"original")
            good = hashlib.sha256(b"original").hexdigest()
            manifest = _manifest_with([FileEntry("w.safetensors", 8, good, True)])
            pipeline._reverify_hashes(manifest, d)  # no raise


def _manifest_with(files: list[FileEntry]) -> Manifest:
    return Manifest(
        model_ref="hf/org/model@" + "0" * 40,
        source_url="u", source_type="hf", repo_id="org/model",
        revision_requested="main", revision_resolved_sha="0" * 40,
        created_at="t", detected_library="transformers", weight_format_selected="safetensors",
        trust_remote_code=False, total_bytes=sum(f.size for f in files), files=files,
        embedded_identity={}, verification=Verification(), storage=StorageBlock(),
    )


class TestManifestRoundTrip(unittest.TestCase):
    def test_json_round_trip(self):
        m = _manifest_with([FileEntry("a", 1, "h", False)])
        m2 = Manifest.from_json(m.to_json())
        self.assertEqual(m2.model_ref, m.model_ref)
        self.assertEqual(m2.files[0].path, "a")


@unittest.skipUnless(os.getenv("MODELVAULT_E2E") == "1", "set MODELVAULT_E2E=1 (needs network + rclone)")
class TestEndToEnd(unittest.TestCase):
    URL = "https://huggingface.co/PaddlePaddle/PP-OCRv6_tiny_det_safetensors"
    REF = "hf/PaddlePaddle/PP-OCRv6_tiny_det_safetensors@07595f982703daf0d4e120a12a01da8073542f3a"

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="mv_e2e_")
        self.conf = os.path.join(self.work, "rclone.conf")
        subprocess.run(
            ["bash", str(ROOT / "ops" / "modelvault_provision.sh"), "--local", self.work, "--conf", self.conf],
            check=True, capture_output=True,
        )
        os.environ["MODELVAULT_RCLONE_CONF"] = self.conf
        os.environ["MODELVAULT_TMP_DIR"] = os.path.join(self.work, "tmp")

    def test_stream_encrypt_restore_idempotent(self):
        from src.modelvault import pipeline
        from src.modelvault.config import load_settings

        s = load_settings()
        m = pipeline.backup(self.URL, s)
        self.assertEqual(m.verification.status, "verified")

        # Ciphertext at rest: the plaintext model_type must not leak into the store.
        leak = subprocess.run(["grep", "-rl", "pp_ocrv6", os.path.join(self.work, "archive")],
                              capture_output=True)
        self.assertEqual(leak.returncode, 1, "plaintext leaked into the encrypted store")

        dest = os.path.join(self.work, "restored")
        pipeline.restore(self.REF, s, dest=dest)  # raises on any sha mismatch
        self.assertTrue(os.path.exists(os.path.join(dest, "model.safetensors")))

        # Idempotent: a second backup is a no-op and the catalog stays single-entry.
        pipeline.backup(self.URL, s)
        self.assertEqual(len(pipeline.list_catalog(s)), 1)


if __name__ == "__main__":
    unittest.main()
