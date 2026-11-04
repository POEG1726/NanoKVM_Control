import csv
import hashlib
import io
import importlib.util
import json
from pathlib import Path, PureWindowsPath
import re
import tempfile
from types import SimpleNamespace
import unittest

from nanokvm_flow.transfers import ArtifactError, pull_test_artifacts


def encode(obj):
    return (json.dumps(obj, indent=2) + "\n").encode()


def descriptor(name, data, role=None):
    obj = {"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    if role:
        obj["role"] = role
    return obj


class FakeTarget:
    root = PureWindowsPath(r"D:\OCBootCapture")

    def __init__(self):
        self.rows = [{"index": i, "result": "failed" if i % 2 else "passed"} for i in range(1, 11)]
        self.data = {f"vt3_run{i}.log": f"original log {i}\r\n".encode() for i in range(1, 11)}
        self.data["test-vt3-summary.json"] = encode({"run_id": "sample", "runs": self.rows, "evidence_complete": True})
        self.data["test-vt3-config.json"] = encode({"repeats": 10, "duration_seconds": 600})
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=["index", "result"])
        writer.writeheader(); writer.writerows(self.rows)
        self.data["test-vt3-summary.csv"] = output.getvalue().encode()
        self.manifest = {"schema_version": 1, "run_id": "sample", "kind": "vt3", "status": "complete",
                         "expected_count": 10, "completed_count": 10, "run_count": 10}
        self.result = {"run_id": "sample", "kind": "vt3", "status": "finished", "runs": self.rows}
        self.seal()
        self.copied = []
        self.corrupt = None
        self.rotate = None
        self.reads = {}

    def seal(self):
        self.manifest["files"] = [descriptor(name, data) for name, data in self.data.items()
                                  if name not in ("test-vt3-artifacts.json", "test-vt3-result.json")]
        self.seal_manifest()

    def seal_manifest(self):
        self.data["test-vt3-artifacts.json"] = encode(self.manifest)
        self.result["manifest"] = descriptor("test-vt3-artifacts.json", self.data["test-vt3-artifacts.json"])
        self.data["test-vt3-result.json"] = encode(self.result)

    def ps(self, code):
        path = PureWindowsPath(re.search(r"ReadAllBytes\('([^']+)'\)", code)[1])
        assert path.parent == self.root / "runs" / "sample"
        name = path.name
        self.reads[name] = self.reads.get(name, 0) + 1
        data = self.data[name]
        if self.rotate == name and self.reads[name] > 1:
            data += b"\n"
        return json.dumps({"content": data.decode(), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})

    def copy(self, remote, local):
        assert remote.parent == self.root / "runs" / "sample"
        self.copied.append(remote.name)
        data = self.data[remote.name]
        Path(local).write_bytes(data + b"CORRUPT" if self.corrupt == remote.name else data)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = SimpleNamespace(name="sample", path=Path(self.tmp.name) / "sample")
        self.run.path.mkdir()
        self.target = FakeTarget()

    def test_complete_failed_batch_is_returned_byte_exact_and_idempotent(self):
        receipt = pull_test_artifacts(self.target, self.run, "vt3")
        self.assertEqual(receipt["run_count"], 10)
        self.assertEqual(len(receipt["files"]), 15)
        self.assertTrue((self.run.path / "received-tests.json").is_file())
        folder = Path(receipt["local_folder"])
        for name, data in self.target.data.items():
            self.assertEqual((folder / name).read_bytes(), data)
        count = len(self.target.copied)
        self.assertEqual(pull_test_artifacts(self.target, self.run, "vt3"), receipt)
        self.assertEqual(len(self.target.copied), count)

    def test_partial_batch_does_not_produce_receipt(self):
        self.target.manifest.update(status="partial", completed_count=9, run_count=9)
        self.target.seal_manifest()
        with self.assertRaises(ArtifactError):
            pull_test_artifacts(self.target, self.run, "vt3")
        self.assertEqual(self.target.copied, [])
        self.assertFalse((self.run.path / "received-tests.json").exists())

    def test_manifest_cannot_count_duplicate_or_missing_logs_as_ten(self):
        self.target.manifest["files"] = [x for x in self.target.manifest["files"] if x["name"] != "vt3_run10.log"]
        self.target.seal_manifest()
        with self.assertRaises(ArtifactError):
            pull_test_artifacts(self.target, self.run, "vt3")

    def test_unsafe_basename_is_rejected_before_download(self):
        for name in ("../vt3_run1.log", r"other\vt3_run1.log", "C:vt3.log", "NUL.txt", "vt3.log."):
            with self.subTest(name=name):
                target = FakeTarget(); target.manifest["files"][0]["name"] = name; target.seal_manifest()
                with self.assertRaises(ArtifactError):
                    pull_test_artifacts(target, self.run, "vt3")
                self.assertEqual(target.copied, [])

    def test_existing_different_original_is_preserved(self):
        folder = self.run.path / "test-artifacts" / "vt3"
        folder.mkdir(parents=True)
        original = folder / "vt3_run1.log"; original.write_bytes(b"preserve me")
        with self.assertRaises(ArtifactError):
            pull_test_artifacts(self.target, self.run, "vt3")
        self.assertEqual(original.read_bytes(), b"preserve me")
        self.assertFalse((self.run.path / "received-tests.json").exists())

    def test_corrupt_transfer_stays_part_and_cannot_complete(self):
        self.target.corrupt = "vt3_run1.log"
        with self.assertRaises(ArtifactError):
            pull_test_artifacts(self.target, self.run, "vt3")
        folder = self.run.path / "test-artifacts" / "vt3"
        self.assertFalse((folder / "vt3_run1.log").exists())
        self.assertTrue(list(folder.glob("vt3_run1.log.*.part")))
        self.assertFalse((self.run.path / "received-tests.json").exists())

    def test_result_manifest_binding_must_match_raw_manifest(self):
        self.target.result["manifest"]["sha256"] = "0" * 64
        self.target.data["test-vt3-result.json"] = encode(self.target.result)
        with self.assertRaises(ArtifactError):
            pull_test_artifacts(self.target, self.run, "vt3")
        self.assertEqual(self.target.copied, [])

    def test_summary_must_really_have_ten_rows(self):
        self.target.data["test-vt3-summary.json"] = encode({"runs": self.target.rows[:9]})
        self.target.seal()
        with self.assertRaises(ArtifactError):
            pull_test_artifacts(self.target, self.run, "vt3")
        self.assertFalse((self.run.path / "received-tests.json").exists())

    def test_remote_rotation_after_transfer_does_not_complete(self):
        self.target.rotate = "test-vt3-artifacts.json"
        with self.assertRaises(ArtifactError):
            pull_test_artifacts(self.target, self.run, "vt3")
        self.assertFalse((self.run.path / "received-tests.json").exists())

    def test_unknown_program_evidence_is_not_converted_into_pass(self):
        self.target.data["test-vt3-summary.json"] = encode({"runs": self.target.rows, "evidence_complete": False})
        self.target.seal()
        receipt = pull_test_artifacts(self.target, self.run, "vt3")
        self.assertFalse(receipt["evidence_complete"])

    def test_wrong_batch_identity_or_run_count_is_rejected(self):
        for key, value in (("run_id", "other"), ("run_count", True), ("expected_count", 9), ("kind", "tm5")):
            with self.subTest(key=key):
                target = FakeTarget(); target.manifest[key] = value; target.seal_manifest()
                with self.assertRaises(ArtifactError):
                    pull_test_artifacts(target, self.run, "vt3")

    def test_explicit_partial_mode_preserves_diagnostics_without_completion_receipt(self):
        target = self.target
        target.rows = target.rows[:2]
        target.result.update(status="failed", runs=target.rows)
        for i in range(3, 11):
            del target.data[f"vt3_run{i}.log"]
        target.data["test-vt3-summary.json"] = encode({"runs": target.rows, "evidence_complete": False})
        target.data["test-vt3-summary.csv"] = b"index,result\n1,failed\n2,passed\n"
        target.manifest.update(status="partial", completed_count=2, run_count=2)
        target.seal()
        receipt = pull_test_artifacts(target, self.run, "vt3", allow_partial=True)
        self.assertFalse(receipt["complete_batch"])
        self.assertEqual(receipt["run_count"], 2)
        self.assertFalse((self.run.path / "received-tests.json").exists())
        self.assertTrue(Path(receipt["receipt_path"]).is_file())
        self.assertEqual((Path(receipt["local_folder"]) / "vt3_run1.log").read_bytes(), target.data["vt3_run1.log"])

    def test_allow_partial_does_not_accept_a_running_batch(self):
        self.target.manifest.update(status="partial", completed_count=0, run_count=0)
        self.target.result.update(status="running", runs=[])
        self.target.seal_manifest()
        with self.assertRaises(ArtifactError):
            pull_test_artifacts(self.target, self.run, "vt3", allow_partial=True)

    def test_contract_with_real_target_artifact_publisher(self):
        source = Path(__file__).resolve().parents[1] / "target" / "vt3_results.py"
        spec = importlib.util.spec_from_file_location("_vt3_transfer_contract", source)
        publisher = importlib.util.module_from_spec(spec); spec.loader.exec_module(publisher)
        folder = Path(self.tmp.name) / "publisher"; folder.mkdir()
        profile = {"expected_count": 10, "repeats": 10, "executable": "fixture.exe", "arguments": ["VT3"]}
        start = {"utc": "2026-11-04T00:00:00+00:00", "uptime_ms": 1, "monotonic_ns": 1}
        end = {"utc": "2026-11-04T00:00:01+00:00", "uptime_ms": 1001, "monotonic_ns": 1000000001}
        rows = []
        for index in range(1, 11):
            log = folder / f"vt3_run{index}.log"
            log.write_bytes(b"Running VT3: Failed\nChecksum Mismatch\nTest Finished\n")
            rows.append(publisher.finish_log(log, index, 1234 + index, start, start, end, 0, profile))
        summary, binding = publisher.publish_artifacts(folder, "sample", profile, rows, [], start, end)
        target = FakeTarget()
        target.data = {file.name: file.read_bytes() for file in folder.iterdir()}
        target.data["test-vt3-result.json"] = encode({"run_id": "sample", "kind": "vt3", "status": "finished", "runs": rows, "manifest": binding})
        receipt = pull_test_artifacts(target, self.run, "vt3")
        self.assertTrue(receipt["complete_batch"])
        self.assertEqual(receipt["run_count"], 10)
        self.assertEqual(_read_summary(Path(receipt["local_folder"]))["counts"]["failed"], 10)


def _read_summary(folder):
    return json.loads((folder / "test-vt3-summary.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
