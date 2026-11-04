import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import contextlib
import io
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "target"))
import pipeline


class TargetPipelineTests(unittest.TestCase):
    def test_run_id_does_not_escape_root(self):
        for bad in ("../escape", "a/b", "x..y", "", "a:b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pipeline.run_folder({"output_root": "runs"}, bad)

    def fixture(self, folder):
        artifact = folder / "dump.bin"
        artifact.write_bytes(b"read-only test dump")
        files = [{"name": artifact.name, "size": artifact.stat().st_size, "sha256": pipeline.sha256(artifact)}]
        manifest = {"run_id": "R1", "status": "complete", "files": files}
        pipeline.write_json(folder / "manifest.json", manifest)
        receipt = {"run_id": "R1", "status": "complete", "manifest_sha256": pipeline.sha256(folder / "manifest.json"), "files": files,
                   "readback_verified": True, "telemetry_verified": {"session": "collector-session", "seq": 2, "timestamp": "2026-11-04T04:00:00Z"}}
        pipeline.write_json(folder / "complete.json", receipt)
        pipeline.write_json(folder / "received.json", receipt)
        return receipt

    def test_matching_host_receipt_and_files_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            self.fixture(p)
            self.assertEqual(pipeline.verify_receipt(p, "R1")["run_id"], "R1")

    def test_host_receipt_wrong_manifest_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            receipt = self.fixture(p)
            receipt["manifest_sha256"] = "0" * 64
            pipeline.write_json(p / "received.json", receipt)
            with self.assertRaisesRegex(RuntimeError, "receipt does not match"):
                pipeline.verify_receipt(p, "R1")

    def test_modified_artifact_rejected_after_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            self.fixture(p)
            (p / "dump.bin").write_bytes(b"changed")
            with self.assertRaisesRegex(RuntimeError, "changed"):
                pipeline.verify_receipt(p, "R1")

    def test_readback_and_receiver_review_required(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            receipt = self.fixture(p)
            receipt["readback_verified"] = False
            pipeline.write_json(p / "received.json", receipt)
            with self.assertRaisesRegex(RuntimeError, "readback review"):
                pipeline.verify_receipt(p, "R1")
            receipt["readback_verified"] = True
            receipt["telemetry_verified"] = True
            pipeline.write_json(p / "received.json", receipt)
            with self.assertRaisesRegex(RuntimeError, "telemetry verification"):
                pipeline.verify_receipt(p, "R1")

    def test_capture_does_not_touch_running_test(self):
        with mock.patch.object(pipeline, "load_config", return_value={"output_root": "runs"}), \
             mock.patch.object(pipeline, "assert_target"), \
             mock.patch.object(pipeline, "test_processes", return_value={"TM5.exe": [1]}), \
             mock.patch.object(pipeline.desktop, "Desktop") as desktop:
            with self.assertRaisesRegex(RuntimeError, "stress test is running"):
                pipeline.capture("unused.json", "R1")
            desktop.assert_not_called()

    def test_capture_afu_is_only_read(self):
        text = (ROOT / "target" / "pipeline.py").read_text(encoding="utf-8")
        self.assertIn('[str(desktop.AFU), str(dump), "/O"]', text)
        self.assertNotIn('"/P"', text)

    def test_installer_does_not_start_task(self):
        text = (ROOT / "target" / "Install-AtLogon.ps1").read_text(encoding="utf-8")
        self.assertIn("Register-ScheduledTask", text)
        self.assertNotIn("Start-ScheduledTask", text)

    def test_missing_pending_is_quiet(self):
        with tempfile.TemporaryDirectory() as d:
            output = io.StringIO()
            with mock.patch.object(sys, "argv", ["pipeline.py", "capture-pending", "--config", "unused", "--pending", str(Path(d) / "missing.json")]), contextlib.redirect_stdout(output):
                pipeline.main()
            self.assertEqual(output.getvalue(), "")

    def test_start_test_uses_interactive_task_and_blocks_repeat(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            folder = root / "R1"
            folder.mkdir()
            self.fixture(folder)
            manifest = pipeline.read_json(folder / "manifest.json")
            manifest["hwtrace"] = {"pid": 1, "executable": str(root / "hwtrace.exe"), "process_spec": "y-cruncher.exe"}
            pipeline.write_json(folder / "manifest.json", manifest)
            receipt = {"run_id": "R1", "status": "complete", "manifest_sha256": pipeline.sha256(folder / "manifest.json"), "files": manifest["files"],
                       "readback_verified": True, "telemetry_verified": {"session": "collector-session", "seq": 2, "timestamp": "2026-11-04T04:00:00Z"}}
            pipeline.write_json(folder / "received.json", receipt)
            pipeline.write_json(folder / "complete.json", receipt)
            exe = root / "y-cruncher.exe"
            exe.touch()
            config = {"output_root": str(root), "tests": {"vt3": {"executable": str(exe), "monitored_processes": ["y-cruncher.exe"]}}}
            with mock.patch.object(pipeline, "load_config", return_value=config), \
                 mock.patch.object(pipeline, "assert_target"), \
                 mock.patch.object(pipeline, "test_processes", return_value={}), \
                 mock.patch.object(pipeline, "process_ids", return_value=[1]), \
                 mock.patch.object(pipeline.desktop, "powershell") as powershell:
                result = pipeline.start_test("config.json", "R1", "vt3")
                self.assertEqual(result["status"], "launch_requested")
                command = powershell.call_args.args[0]
                self.assertIn("-LogonType Interactive", command)
                self.assertIn("Start-ScheduledTask", command)
                with self.assertRaisesRegex(RuntimeError, "already has a launch"):
                    pipeline.start_test("config.json", "R1", "vt3")
                self.assertEqual(powershell.call_count, 1)

    def test_expired_trace_requires_new_receiver_receipt_before_test(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            folder = root / "R1"
            folder.mkdir()
            receipt = self.fixture(folder)
            manifest = pipeline.read_json(folder / "manifest.json")
            old = {"pid": 1, "executable": str(root / "hwtrace.exe"), "process_spec": "y-cruncher.exe"}
            manifest["hwtrace"] = old
            pipeline.write_json(folder / "manifest.json", manifest)
            receipt["manifest_sha256"] = pipeline.sha256(folder / "manifest.json")
            pipeline.write_json(folder / "complete.json", receipt)
            pipeline.write_json(folder / "received.json", receipt)
            exe = root / "y-cruncher.exe"
            exe.touch()
            config = {"output_root": str(root), "tests": {"vt3": {"executable": str(exe), "monitored_processes": ["y-cruncher.exe"]}}}
            new = dict(old, pid=2)
            with mock.patch.object(pipeline, "load_config", return_value=config), \
                 mock.patch.object(pipeline, "assert_target"), \
                 mock.patch.object(pipeline, "test_processes", return_value={}), \
                 mock.patch.object(pipeline, "process_ids", return_value=[]), \
                 mock.patch.object(pipeline, "launch_trace", return_value=new), \
                 mock.patch.object(pipeline.desktop, "powershell") as powershell:
                result = pipeline.start_test("config.json", "R1", "vt3")
                self.assertEqual(result["status"], "awaiting_receiver")
                self.assertEqual(result["hwtrace"]["pid"], 2)
                powershell.assert_not_called()
                self.assertFalse((folder / "test-vt3.lock").exists())
                with self.assertRaisesRegex(RuntimeError, "receipt does not match"):
                    pipeline.verify_receipt(folder, "R1")


if __name__ == "__main__":
    unittest.main()
