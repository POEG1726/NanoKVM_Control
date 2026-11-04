import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from nanokvm_flow.notify import Notifier
from nanokvm_flow.state import Run


THREAD = "00000000-0000-4000-8000-000000000001"


class NotificationTests(unittest.TestCase):
    def setup_run(self, root):
        run = Run(root, "R1")
        run.create({}, 0)
        run.update(stage="awaiting_f10_review")
        evidence = run.path / "f10.jpg"
        evidence.write_bytes(b"test evidence")
        return run, evidence

    def notifier(self, **kwargs):
        return Notifier({"notifications": {"enabled": True, "thread_id": THREAD, "retry_seconds": 0, **kwargs}})

    def test_success_is_persistent_and_unchanged_poll_deduplicates(self):
        with tempfile.TemporaryDirectory() as root:
            run, evidence = self.setup_run(root)
            with mock.patch("nanokvm_flow.notify.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "queued", "")) as call:
                first = self.notifier().notify(run, "f10", "review F10", evidence)
                again = self.notifier().notify(run, "f10", "new polling wording", evidence)
            self.assertEqual(first["status"], "queued")
            self.assertEqual(again["status"], "already_queued")
            self.assertEqual(call.call_count, 1)
            argv = call.call_args.args[0]
            self.assertEqual(argv[1:4], ["queue", "--thread", THREAD])
            self.assertFalse(call.call_args.kwargs["shell"])
            self.assertEqual(self.notifier().outbox(run), [])

    def test_failed_queue_remains_pending_then_drain_retries(self):
        with tempfile.TemporaryDirectory() as root:
            run, evidence = self.setup_run(root)
            with mock.patch("nanokvm_flow.notify.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "secret diagnostics")):
                result = self.notifier().notify(run, "f10", "review F10", evidence)
            self.assertEqual(result["status"], "pending")
            self.assertEqual(len(self.notifier().outbox(run)), 1)
            raw = next((run.path / "notifications").glob("*.json")).read_text()
            self.assertNotIn("secret diagnostics", raw)
            with mock.patch("nanokvm_flow.notify.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "queued", "")):
                retried = self.notifier().drain(run, force=True)
            self.assertEqual(retried[0]["status"], "queued")

    def test_dry_run_never_sends_or_marks_queued(self):
        with tempfile.TemporaryDirectory() as root:
            run, evidence = self.setup_run(root)
            with mock.patch("nanokvm_flow.notify.subprocess.run") as call:
                result = self.notifier().notify(run, "f10", "review F10", evidence, dry_run=True)
            call.assert_not_called()
            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(len(self.notifier().outbox(run)), 1)

    def test_sensitive_fields_and_text_never_enter_message(self):
        with tempfile.TemporaryDirectory() as root:
            run, evidence = self.setup_run(root)
            run.update(expected_changes={"VDDP": 0.9, "password": "do-not-send"})
            with mock.patch("nanokvm_flow.notify.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as call:
                self.notifier().notify(run, "f10", "Bearer do-not-send", evidence)
            message = call.call_args.args[0][-1]
            self.assertNotIn("do-not-send", message)
            self.assertNotIn('"password"', message)

    def test_changed_evidence_produces_new_event(self):
        with tempfile.TemporaryDirectory() as root:
            run, evidence = self.setup_run(root)
            with mock.patch("nanokvm_flow.notify.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as call:
                first = self.notifier().notify(run, "f10", "review F10", evidence)
                evidence.write_bytes(b"changed evidence")
                second = self.notifier().notify(run, "f10", "review F10", evidence)
            self.assertNotEqual(first["event_id"], second["event_id"])
            self.assertEqual(call.call_count, 2)

    def test_invalid_destination_stays_pending_without_subprocess(self):
        with tempfile.TemporaryDirectory() as root:
            run, evidence = self.setup_run(root)
            with mock.patch("nanokvm_flow.notify.subprocess.run") as call:
                result = self.notifier(thread_id="last").notify(run, "f10", "review", evidence)
            call.assert_not_called()
            self.assertEqual(result["status"], "pending")

    def test_timeout_remains_retryable(self):
        with tempfile.TemporaryDirectory() as root:
            run, evidence = self.setup_run(root)
            with mock.patch("nanokvm_flow.notify.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 20)):
                result = self.notifier().notify(run, "f10", "review", evidence)
            self.assertEqual(result["status"], "pending")
            self.assertEqual(result["error"]["type"], "TimeoutExpired")


if __name__ == "__main__":
    unittest.main()
