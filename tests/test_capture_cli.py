import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "target"))
import desktop_capture as desktop
import pipeline


class Clock:
    value = 0
    def now(self): return self.value
    def sleep(self, seconds): self.value += seconds


def snapshot(mode="one_shot_cli"):
    return {"capture_mode": mode,
            "static": {x: {} for x in ("system", "memory", "spd")},
            "config": {x: {} for x in ("timings", "aod", "apob")},
            "readings": {x: {} for x in ("power_table", "sensors", "dimm")}}


class CaptureCLITests(unittest.TestCase):
    def test_existing_hwinfo_and_valid_sm2_are_reused_without_gui_or_launch(self):
        launch = mock.Mock()
        with mock.patch.object(desktop, "Desktop") as ui:
            result = desktop.ensure_hwinfo({}, lambda name: [1404] if name == "hwinfo64.exe" else [],
                                          probe=lambda: {"ready": True}, launch=launch)
        self.assertEqual(result["mode"], "reused")
        self.assertEqual(result["process_ids"], [1404])
        launch.assert_not_called(); ui.assert_not_called()

    def test_existing_process_waits_for_sm2_without_second_instance(self):
        clock = Clock(); launch = mock.Mock()
        result = desktop.ensure_hwinfo({"hwinfo_ready_timeout_seconds": 2}, lambda _: [1404],
                                      probe=lambda: {"ready": clock.value >= 1}, launch=launch,
                                      clock=clock.now, sleep=clock.sleep)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["waited_seconds"], 1)
        launch.assert_not_called()

    def test_existing_process_missing_sm2_times_out_without_click_or_restart(self):
        clock = Clock(); launch = mock.Mock()
        with mock.patch.object(desktop, "Desktop") as ui:
            with self.assertRaises(desktop.HWiNFOReadyError) as caught:
                desktop.ensure_hwinfo({"hwinfo_ready_timeout_seconds": 1}, lambda _: [1404],
                                      probe=lambda: {"ready": False, "reason": "mapping_unavailable"},
                                      launch=launch, clock=clock.now, sleep=clock.sleep)
        self.assertEqual(caught.exception.diagnostic["status"], "failed")
        self.assertEqual(caught.exception.diagnostic["process_ids"], [1404])
        launch.assert_not_called(); ui.assert_not_called()

    def test_only_missing_process_and_mapping_launch_once(self):
        clock = Clock(); launched = []
        result = desktop.ensure_hwinfo({}, lambda _: [1404] if launched else [],
                                      probe=lambda: {"ready": bool(launched)},
                                      launch=lambda cfg: launched.append(True), clock=clock.now, sleep=clock.sleep)
        self.assertEqual(result["mode"], "started_missing_instance")
        self.assertEqual(len(launched), 1)

    def test_mapping_without_known_process_does_not_spawn_duplicate(self):
        launch = mock.Mock()
        with self.assertRaises(desktop.HWiNFOReadyError):
            desktop.ensure_hwinfo({}, lambda _: [], probe=lambda: {"ready": True}, launch=launch)
        launch.assert_not_called()

    def test_sm2_signature_sizes_ranges_and_overlap_are_checked(self):
        header = bytearray(48)
        struct.pack_into("<III", header, 0, 0x53695748, 2, 0)
        struct.pack_into("<IIIIII", header, 20, 48, 264, 1, 312, 316, 1)
        self.assertTrue(desktop.validate_sm2_header(header, 628)["ready"])
        self.assertFalse(desktop.validate_sm2_header(header, 627)["ready"])
        overlapping = bytearray(header); struct.pack_into("<I", overlapping, 32, 100)
        self.assertFalse(desktop.validate_sm2_header(overlapping, 628)["ready"])
        bad = bytearray(header); struct.pack_into("<I", bad, 0, 0)
        self.assertFalse(desktop.validate_sm2_header(bad, 628)["ready"])
        self.assertFalse(desktop.validate_sm2_header(header[:44], 628)["ready"])

    def cli_fixture(self, directory):
        root = Path(directory); executable = root / "ZenTimings.Export.exe"; executable.write_bytes(b"offline fake")
        return {"zentimings_cli": {"executable": str(executable), "timeout_seconds": 180}}, root / "snapshot.json"

    def test_cli_is_called_exactly_once_and_requires_complete_live_json(self):
        with tempfile.TemporaryDirectory() as d:
            config, output = self.cli_fixture(d)
            def fake(command, **kwargs):
                output.write_text(json.dumps(snapshot()), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, '{"status":"complete"}', "")
            with mock.patch.object(desktop.subprocess, "run", side_effect=fake) as call, \
                 mock.patch.object(desktop, "Desktop") as ui:
                result = desktop.export_zen_cli(config, output)
            self.assertEqual(call.call_count, 1)
            self.assertEqual(call.call_args.args[0][1:], ["--export-json", str(output), "--exit"])
            self.assertEqual(result["mode"], "one_shot_cli")
            ui.assert_not_called()

    def test_legacy_gui_config_has_no_fallback(self):
        with mock.patch.object(desktop.subprocess, "run") as call:
            with self.assertRaisesRegex(ValueError, "GUI export is unsupported"):
                desktop.export_zen_cli({"zentimings": "old-gui"}, Path("unused"))
            call.assert_not_called()

    def test_cli_synthetic_or_incomplete_output_is_rejected(self):
        for mode, missing in (("synthetic_offline_test", False), ("one_shot_cli", True)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as d:
                config, output = self.cli_fixture(d)
                def fake(command, **kwargs):
                    value = snapshot(mode)
                    if missing: value["config"].pop("apob")
                    output.write_text(json.dumps(value), encoding="utf-8")
                    return subprocess.CompletedProcess(command, 0, "", "")
                with mock.patch.object(desktop.subprocess, "run", side_effect=fake):
                    with self.assertRaises(RuntimeError): desktop.export_zen_cli(config, output)

    def test_cli_failure_and_existing_destination_do_not_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            config, output = self.cli_fixture(d)
            with mock.patch.object(desktop.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "device unavailable")) as call:
                with self.assertRaisesRegex(RuntimeError, "exit code 1"):
                    desktop.export_zen_cli(config, output)
            self.assertEqual(call.call_count, 1)
        with tempfile.TemporaryDirectory() as d:
            config, output = self.cli_fixture(d); output.write_bytes(b"original")
            with mock.patch.object(desktop.subprocess, "run") as call:
                with self.assertRaises(FileExistsError): desktop.export_zen_cli(config, output)
                call.assert_not_called()
            self.assertEqual(output.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
