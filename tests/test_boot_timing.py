import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from nanokvm_flow.boot_timing import adopt_legacy, begin, desktop_ready, observe


BASE = dt.datetime(2026, 11, 4, 6, 0, tzinfo=dt.timezone.utc)
BASE_NS = 100_000_000_000


def stamp(seconds):
    return (BASE + dt.timedelta(seconds=seconds)).isoformat()


class FakeRun:
    name = "timing-fixture"

    def __init__(self, path, state=None):
        self.path = Path(path)
        self.state = state or {}
        self.events = []

    def load(self):
        return copy.deepcopy(self.state)

    def event(self, event, **fields):
        self.events.append((event, fields))


class BootTimingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = FakeRun(self.tmp.name)

    def start(self):
        return begin(self.run, stamp(0), BASE_NS, stamp(.1), BASE_NS + 100_000_000)

    def poll(self, available, start, end):
        return observe(self.run, available, stamp(start), stamp(end),
                       BASE_NS + int(start * 1e9), BASE_NS + int(end * 1e9), {"source": "fixture"})

    def test_loss_then_restoration_retains_poll_uncertainty(self):
        self.start()
        self.poll(True, 1, 1.2)
        self.poll(False, 2, 2.2)
        self.poll(False, 5, 5.2)
        result = self.poll(True, 7, 7.2)
        self.assertAlmostEqual(result["boot_duration_s"], 7.2)
        self.assertEqual(result["boot_duration_basis"], "monotonic")
        endpoint = result["endpoints"]["first_signal_restored"]
        self.assertAlmostEqual(endpoint["physical_transition_elapsed_lower_s"], 5)
        self.assertAlmostEqual(endpoint["physical_transition_elapsed_upper_s"], 7.2)
        self.assertAlmostEqual(endpoint["detection_lag_upper_bound_s"], 2.2)
        self.assertEqual(endpoint["previous_observation"]["available"], False)
        self.assertEqual(result["observations"]["count"], 4)
        self.assertEqual(len(result["observations"]["transitions"]), 3)

    def test_initial_existing_picture_is_not_a_restoration(self):
        self.start()
        result = self.poll(True, 2, 2.1)
        self.assertIsNone(result["endpoints"]["first_signal_restored"])
        self.assertIsNone(result["boot_duration_s"])
        self.assertEqual(result["status"], "awaiting_signal_absence")

    def test_initial_absence_is_not_an_exact_loss_edge(self):
        self.start()
        result = self.poll(False, 2, 2.1)
        lost = result["endpoints"]["first_signal_lost"]
        self.assertEqual(lost["event"], "initial_absence")
        self.assertIsNone(lost["physical_transition_elapsed_lower_s"])
        result = self.poll(True, 10, 10.2)
        self.assertAlmostEqual(result["boot_duration_s"], 10.2)

    def test_no_total_timeout_and_no_implicit_post_failure(self):
        self.start()
        self.poll(False, 1, 1.1)
        result = self.poll(False, 24 * 60 * 60, 24 * 60 * 60 + .2)
        self.assertEqual(result["status"], "awaiting_signal_restoration")
        self.assertIsNone(result["boot_duration_s"])
        self.assertNotIn("failed", result)

    def test_first_restore_is_not_replaced_by_a_later_edge(self):
        self.start(); self.poll(False, 1, 1.1); self.poll(True, 2, 2.1)
        self.poll(False, 3, 3.1)
        result = self.poll(True, 4, 4.1)
        self.assertAlmostEqual(result["boot_duration_s"], 2.1)
        self.assertEqual(len(result["observations"]["transitions"]), 4)

    def test_legacy_only_uses_recorded_save_time(self):
        self.run.state = {"save_sent_utc": stamp(0), "stage": "waiting_windows", "created_utc": stamp(-999)}
        previous = copy.deepcopy(self.run.state)
        result = adopt_legacy(self.run)
        self.assertEqual(result["origin"], "legacy_save_sent_utc")
        self.assertIsNone(result["start"]["command_start_monotonic_ns"])
        self.assertIsNone(result["start"]["ack_utc"])
        observe(self.run, False, stamp(1), stamp(1.2), BASE_NS, BASE_NS + 200_000_000)
        result = observe(self.run, True, stamp(3), stamp(3.2), BASE_NS + 2_000_000_000, BASE_NS + 2_200_000_000)
        self.assertEqual(result["boot_duration_basis"], "utc_fallback")
        self.assertAlmostEqual(result["boot_duration_s"], 3.2)
        self.assertEqual(previous, self.run.state)

    def test_monotonic_clock_wins_over_wall_clock_correction(self):
        self.start(); self.poll(False, 1, 1.2)
        result = observe(self.run, True, stamp(-60), stamp(-59.8), BASE_NS + 7_000_000_000, BASE_NS + 7_200_000_000)
        self.assertAlmostEqual(result["boot_duration_s"], 7.2)
        self.assertEqual(result["boot_duration_basis"], "monotonic")

    def test_negative_utc_fallback_is_not_reported_as_physical_duration(self):
        self.run.state = {"save_sent_utc": stamp(0)}
        adopt_legacy(self.run)
        observe(self.run, False, stamp(1), stamp(1.2))
        result = observe(self.run, True, stamp(-60), stamp(-59.8))
        self.assertIsNone(result["boot_duration_s"])
        self.assertIn("clock_anomaly", result["endpoints"]["first_signal_restored"])
        self.assertAlmostEqual(result["endpoints"]["first_signal_restored"]["raw_elapsed_s"], -59.8)

    def test_desktop_ready_is_separate_and_does_not_manufacture_restore(self):
        self.start()
        result = desktop_ready(self.run, stamp(20), BASE_NS + 20_000_000_000)
        self.assertAlmostEqual(result["endpoints"]["desktop_ready"]["elapsed_s"], 20)
        self.assertIsNone(result["endpoints"]["first_signal_restored"])
        self.assertIsNone(result["boot_duration_s"])
        self.assertEqual(desktop_ready(self.run, stamp(30))["endpoints"]["desktop_ready"]["elapsed_s"], 20)

    def test_origin_is_idempotent_but_cannot_silently_change(self):
        self.start(); self.poll(False, 1, 1.1)
        self.assertEqual(self.start()["observations"]["count"], 1)
        with self.assertRaises(FileExistsError):
            begin(self.run, stamp(5), BASE_NS + 5_000_000_000, stamp(5.1), BASE_NS + 5_100_000_000)

    def test_missing_legacy_origin_is_not_inferred_from_created_time(self):
        self.run.state = {"created_utc": stamp(0)}
        self.assertIsNone(adopt_legacy(self.run))
        with self.assertRaises(RuntimeError):
            self.poll(False, 2, 2.1)
        self.assertFalse((self.run.path / "boot-timing.json").exists())

    def test_reversed_monotonic_interval_is_rejected(self):
        self.start()
        with self.assertRaises(ValueError):
            observe(self.run, False, stamp(1), stamp(1.2), 20, 10)
        with self.assertRaises(ValueError):
            observe(self.run, 1, stamp(1), stamp(1.2))

    def test_only_timing_file_and_edge_events_are_written(self):
        self.run.state = {"stage": "waiting_windows"}
        self.start(); self.poll(False, 1, 1.1); result = self.poll(True, 2, 2.1)
        self.assertEqual(self.run.state, {"stage": "waiting_windows"})
        self.assertEqual(len(self.run.events), 2)
        disk = json.loads((self.run.path / "boot-timing.json").read_text(encoding="utf-8"))
        self.assertEqual(disk, result)
        self.assertIn("not measured physical BIOS exit", disk["start"]["scope"])


if __name__ == "__main__":
    unittest.main()
