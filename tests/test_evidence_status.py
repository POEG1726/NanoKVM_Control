import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from nanokvm_flow.evidence_status import data_sources
from nanokvm_flow.notify import Notifier
from nanokvm_flow.state import Run


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class EvidenceStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = Run(self.root / "runs", "R1"); self.run.create({}, 0)
        self.run.update(stage="test_finished", save_sent_utc="2026-11-04T05:07:07Z",
                        telemetry={"session": "same-boot-session"})
        self.config = {"telemetry_db": str(self.root / "telemetry.sqlite3"),
                       "notifications": {"enabled": True,
                                         "thread_id": "00000000-0000-4000-8000-000000000001"}}

    def exports(self):
        folder = self.run.path / "target"; folder.mkdir(exist_ok=True)
        files = []
        for name, value in (("dump.bin", b"offline dump"), ("ZenTimings.json", b'{"offline":true}')):
            (folder / name).write_bytes(value)
            files.append({"name": name, "size": len(value), "sha256": hashlib.sha256(value).hexdigest()})
        write(folder / "manifest.json", {"files": files})

    def telemetry(self):
        with closing(sqlite3.connect(self.config["telemetry_db"])) as db:
            db.execute("create table records(session text,kind text,timestamp text)")
            db.executemany("insert into records values(?,?,?)", [
                ("same-boot-session", "sample", "2026-11-04T05:10:00Z"),
                ("same-boot-session", "sample", "2026-11-04T05:10:01Z"),
                ("another-session", "sample", "2026-11-04T06:00:00Z"),
                ("same-boot-session", "process_start", "2026-11-04T05:09:00Z")])
            db.commit()

    def capture(self, folder=None):
        folder = folder or self.run.path
        captured = folder / "captured"; captured.mkdir(parents=True)
        (captured / "transactions_000001.tsv.zst").write_bytes(b"offline stored recorder output")
        write(folder / "dsview-start.json", {"state": {"output_directory": str(captured)}})
        write(folder / "dsview-stop.json", {"status": "stopped_and_flushed", "state": {
            "output_directory": str(captured), "flushed": True, "recorder_failed": False,
            "queued_bytes": 0, "recorder_running": False, "working": False,
            "format": "espi", "samples_written": 128}})
        return captured

    def sources(self):
        return data_sources(self.config, self.run.path, self.run.load())

    def test_available_files_are_hash_verified_and_telemetry_is_session_scoped(self):
        self.exports(); self.telemetry(); self.capture()
        result = self.sources()
        self.assertTrue(all(item["available"] for item in result.values()))
        self.assertEqual(result["hwtrace"]["sample_count"], 2)
        self.assertEqual(result["hwtrace"]["last_sample_utc"], "2026-11-04T05:10:01Z")
        self.assertIn("not_a_live_process_claim", result["hwtrace"]["scope"])
        self.assertEqual(result["dump"]["sha256"], hashlib.sha256(b"offline dump").hexdigest())

    def test_changed_dump_is_not_claimed_available(self):
        self.exports()
        (self.run.path / "target" / "dump.bin").write_bytes(b"modified")
        result = self.sources()
        self.assertFalse(result["dump"]["available"])
        self.assertTrue(result["zentimings"]["available"])

    def test_unrelated_smoke_capture_is_not_claimed_as_boot_capture(self):
        self.capture(self.root / "runs" / "dsview-cli-smoke")
        result = self.sources()["dsview_capture"]
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "no_capture_linked_to_this_boot")

    def test_parent_capture_requires_the_same_f10_origin(self):
        parent = self.root / "runs" / "parent"
        self.capture(parent)
        write(parent / "state.json", {"save_sent_utc": "2026-11-04T05:07:07+00:00"})
        self.run.update(parent_run_id="parent")
        self.assertTrue(self.sources()["dsview_capture"]["available"])
        self.assertEqual(self.sources()["dsview_capture"]["source_run_id"], "parent")
        write(parent / "state.json", {"save_sent_utc": "2026-11-04T05:00:00Z"})
        self.assertFalse(self.sources()["dsview_capture"]["available"])

    def test_partial_capture_files_prevent_available_claim(self):
        captured = self.capture()
        (captured / "record.part").write_bytes(b"incomplete")
        self.assertEqual(self.sources()["dsview_capture"]["reason"], "partial_capture_files_remain")

    def test_invalid_metadata_yields_unavailable_without_breaking_other_checks(self):
        self.exports(); self.telemetry()
        (self.run.path / "target" / "manifest.json").write_text("not JSON")
        result = self.sources()
        self.assertFalse(result["dump"]["available"])
        self.assertTrue(result["hwtrace"]["available"])

    def test_result_notification_includes_all_four_sources_and_readable_summary(self):
        self.exports(); self.telemetry()
        result = Notifier(self.config).notify(self.run, "test_finished", "Ten runs received.", [], dry_run=True)
        payload = result["payload"]
        self.assertEqual(set(payload["data_sources"]), {"dump", "zentimings", "dsview_capture", "hwtrace"})
        self.assertIn("dump可用", payload["summary"])
        self.assertIn("DSView capture缺失或未验证", payload["summary"])
        self.assertTrue(Path(payload["data_sources"]["zentimings"]["path"]).is_file())

    def test_new_telemetry_rows_do_not_duplicate_the_same_completion_event(self):
        self.exports(); self.telemetry()
        notifier = Notifier(self.config)
        with patch("nanokvm_flow.notify.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "queued", "")) as send:
            first = notifier.notify(self.run, "test_finished", "Ten runs received.", [])
            with closing(sqlite3.connect(self.config["telemetry_db"])) as db:
                db.execute("insert into records values(?,?,?)", ("same-boot-session", "sample", "2026-11-04T05:10:02Z"))
                db.commit()
            again = notifier.notify(self.run, "test_finished", "Ten runs received.", [])
        self.assertEqual(first["event_id"], again["event_id"])
        self.assertEqual(again["status"], "already_queued")
        send.assert_called_once()


if __name__ == "__main__": unittest.main()
