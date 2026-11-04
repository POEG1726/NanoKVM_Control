import csv
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "target"))
import test_runner
import vt3_results


HEADER = """Component Stress Tester
  1   Logical Cores:      32
  2   Memory:              116 GiB (3.63 GiB per thread)
  4/5 Time Limit:         600 seconds per test / 600 seconds total
Iteration: 0 Total Elapsed Time: 0.021 seconds
"""
PASS = HEADER + "Running VT3: Passed\nTest Finished. Waiting for threads to terminate...\n"
FAIL = HEADER + "Exception Encountered: ConvolutionFailedException\nCoefficient is too large.\nError(s) encountered on logical core 19.\nRunning VT3: Failed\nStress test failed with 1 error.\nTest Finished.\n"


def profile():
    return {"executable": "y-cruncher.exe", "working_directory": ".",
            "arguments": ["pause:-2", "logfile:{log}", "stress", "-M:116G", "-D:600", "-TL:600", "VT3"],
            "repeats": 10, "expected_count": 10, "gap_seconds": 15}


class FakeClock:
    def __init__(self):
        self.ns = 100_000_000_000
        self.wall_offset = 0
        self.origin = dt.datetime(2026, 11, 4, tzinfo=dt.timezone.utc)
        self.sleeps = []

    def sample(self):
        return {"utc": (self.origin + dt.timedelta(seconds=self.ns / 1e9 + self.wall_offset)).isoformat(),
                "uptime_ms": self.ns // 1_000_000, "monotonic_ns": self.ns}

    def advance(self, seconds):
        self.ns += int(seconds * 1e9)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


class FakePopen:
    def __init__(self, clock, *, fail_indexes=(), missing_indexes=(), startup_failure=None, wall_jump=None,
                 exit_codes=None):
        self.clock = clock
        self.calls = []
        self.fail_indexes = set(fail_indexes)
        self.missing_indexes = set(missing_indexes)
        self.startup_failure = startup_failure
        self.wall_jump = wall_jump
        self.exit_codes = exit_codes or {}
        self.published_starts = []

    def __call__(self, command, **kwargs):
        index = len(self.calls) + 1
        self.calls.append((command, kwargs))
        if self.startup_failure == index:
            raise FileNotFoundError("test executable missing")
        clock = self.clock
        owner = self
        logfile = Path(next(a[len("logfile:"):] for a in command if a.startswith("logfile:")))
        clock.advance(.25)

        class Process:
            pid = 1000 + index

            def wait(self):
                owner.published_starts.append(json.loads((logfile.parent / "test.json").read_text()))
                clock.advance(2 if index in owner.fail_indexes else 599.75)
                if owner.wall_jump == index:
                    clock.wall_offset += 120
                if index not in owner.missing_indexes:
                    logfile.write_bytes((FAIL if index in owner.fail_indexes else PASS).encode("utf-8"))
                return owner.exit_codes.get(index, 0)

        return Process()


class VT3ResultTests(unittest.TestCase):
    def test_ten_launches_continue_after_algorithm_failure_and_publish_all_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d); clock = FakeClock(); popen = FakePopen(clock, fail_indexes={1, 3, 9})
            result = test_runner.run_vt3(p, "R1", profile(), popen=popen, clock=clock, sleep=clock.sleep)
            self.assertEqual(result["status"], "finished")
            self.assertEqual(result["completed_count"], 10)
            self.assertEqual(len(popen.calls), 10)
            self.assertEqual([x["pid"] for x in popen.published_starts], list(range(1001, 1011)))
            self.assertTrue(all(x["status"] == "process_started" for x in popen.published_starts))
            self.assertEqual(result["counts"], {"passed": 7, "failed": 3, "unknown": 0, "program_error": 0})
            self.assertFalse(result["passed"])
            self.assertEqual(clock.sleeps, [15] * 9)
            self.assertEqual(result["total_gap_duration_s"], 135)
            self.assertEqual(result["runs"][0]["duration_s"], 2.25)
            self.assertEqual(result["runs"][1]["duration_s"], 600)
            self.assertEqual(result["runs"][0]["failure_detected_threads"], [19])
            self.assertEqual(result["runs"][0]["error_types"], ["CoefficientTooLarge"])
            self.assertEqual(result["runs"][1]["gap_before_s"], 15)
            self.assertEqual(result["runs"][-1]["gap_after_s"], 0)
            manifest = json.loads((p / "test-vt3-artifacts.json").read_text())
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(len([x for x in manifest["files"] if x["role"] == "log"]), 10)
            for item in manifest["files"]:
                artifact = p / item["name"]
                self.assertEqual(artifact.stat().st_size, item["size"])
                self.assertEqual(vt3_results.digest(artifact), item["sha256"])
            self.assertEqual(vt3_results.digest(p / result["manifest"]["name"]), result["manifest"]["sha256"])
            with (p / "test-vt3-summary.csv").open(encoding="utf-8", newline="") as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 10)
            self.assertEqual(json.loads((p / "test-vt3-summary.json").read_text())["run_count"], 10)
            self.assertTrue((p / "vt3_run1.log").read_text().startswith(FAIL))
            self.assertIn("[Runner]", (p / "vt3_run1.log").read_text())
            self.assertEqual(result["runs"][0]["program_log_sha256"], hashlib.sha256(FAIL.encode()).hexdigest())

    def test_missing_program_log_is_unknown_but_batch_continues(self):
        with tempfile.TemporaryDirectory() as d:
            clock = FakeClock(); popen = FakePopen(clock, missing_indexes={2})
            result = test_runner.run_vt3(d, "R1", profile(), popen=popen, clock=clock, sleep=clock.sleep)
            self.assertEqual(len(popen.calls), 10)
            self.assertEqual(result["status"], "finished")
            self.assertEqual(result["runs"][1]["result"], "unknown")
            self.assertFalse(result["runs"][1]["program_log_present"])
            self.assertFalse(result["evidence_complete"])
            self.assertFalse(result["passed"])
            self.assertTrue((Path(d) / "vt3_run2.log").is_file())

    def test_real_startup_failure_publishes_partial_manifest_and_stops(self):
        with tempfile.TemporaryDirectory() as d:
            clock = FakeClock(); popen = FakePopen(clock, startup_failure=3)
            result = test_runner.run_vt3(d, "R1", profile(), popen=popen, clock=clock, sleep=clock.sleep)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["completed_count"], 2)
            self.assertEqual(len(popen.calls), 3)
            self.assertFalse(result["count_complete"])
            self.assertEqual(json.loads((Path(d) / "test-vt3-artifacts.json").read_text())["status"], "partial")

    def test_monotonic_duration_survives_wall_clock_jump(self):
        with tempfile.TemporaryDirectory() as d:
            clock = FakeClock(); popen = FakePopen(clock, fail_indexes={1}, wall_jump=1)
            result = test_runner.run_vt3(d, "R1", profile(), popen=popen, clock=clock, sleep=clock.sleep)
            first = result["runs"][0]
            self.assertEqual(first["duration_s"], 2.25)
            self.assertEqual(first["wall_duration_s"], 122.25)
            self.assertEqual(first["end_uptime_ms"] - first["start_uptime_ms"], 2250)
            self.assertFalse(first["program_total_elapsed_used"])

    def test_exit_zero_alone_and_previous_pass_do_not_override_failure(self):
        self.assertEqual(vt3_results.parse_log("", 0)["result"], "unknown")
        self.assertEqual(vt3_results.parse_log(PASS + FAIL, 0)["result"], "failed")
        self.assertEqual(vt3_results.parse_log(PASS, 5)["result"], "program_error")
        self.assertEqual(vt3_results.parse_log("Exception Encountered: OutOfMemoryException", 0)["result"], "program_error")

    def test_profile_requires_ten_runs_and_600_second_limits(self):
        p = profile(); p["repeats"] = 3
        with self.assertRaises(ValueError): vt3_results.validate_profile(p)
        p = profile(); p["expected_count"] = 9
        with self.assertRaises(ValueError): vt3_results.validate_profile(p)
        p = profile(); p["arguments"].remove("-TL:600")
        with self.assertRaises(ValueError): vt3_results.validate_profile(p)
        p = profile(); p["arguments"].append("-TL:120")
        with self.assertRaises(ValueError): vt3_results.validate_profile(p)

    def test_finished_is_published_only_after_artifact_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d); clock = FakeClock(); popen = FakePopen(clock)
            original = test_runner.pipeline.write_json
            checked = []
            def writer(path, value):
                if path.name == "test-vt3-result.json" and value.get("status") == "finished":
                    manifest = folder / value["manifest"]["name"]
                    self.assertTrue(manifest.is_file())
                    self.assertEqual(vt3_results.digest(manifest), value["manifest"]["sha256"])
                    checked.append(True)
                original(path, value)
            with mock.patch.object(test_runner.pipeline, "write_json", side_effect=writer):
                test_runner.run_vt3(folder, "R1", profile(), popen=popen, clock=clock, sleep=clock.sleep)
            self.assertEqual(checked, [True])

    def test_existing_log_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "vt3_run1.log"; path.write_bytes(b"original")
            with self.assertRaises(FileExistsError): test_runner.run_vt3(d, "R1", profile())
            self.assertEqual(path.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
