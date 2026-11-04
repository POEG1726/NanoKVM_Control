import json
import unittest
from copy import deepcopy
from pathlib import Path

from nanokvm_flow.bios import ProfileError, anchor_review_request, build_actions, expected_changes, profile_sha256, review_request, save_profile_actions, validate_profile


def profile():
    return json.loads((Path(__file__).resolve().parents[1] / "profiles/b850m_f12d.json").read_text(encoding="utf-8"))


def calibrated():
    p = profile()
    p["calibration"].update(verified=True, evidence=["offline-test-fixture-not-hardware-calibration"])
    p["route"]["navigation_actions"] = [{"op": "key", "keys": ["ArrowRight"], "count": 1}]
    p["route"]["open_field_actions"] = [{"op": "key", "keys": ["Enter"], "count": 1}]
    p["route"]["selector"] = {"mode": "list_index", "home_selects_first_option": True, "option_indices": [2, 12, 22, 32, 42]}
    return p


class BiosPlanTests(unittest.TestCase):
    def test_uncalibrated_profile_cannot_emit_keys(self):
        p = profile()
        p["calibration"]["verified"] = False
        validate_profile(p)
        with self.assertRaises(ProfileError):
            build_actions(p, 0)
        preview = build_actions(p, 0, allow_uncalibrated=True)
        self.assertEqual(preview[0]["phase"], "needs_calibration")
        self.assertFalse(any(a["op"] == "key" for a in preview))

    def test_live_profile_has_measured_direct_numeric_route(self):
        p = profile()
        validate_profile(p)
        self.assertTrue(p["calibration"]["verified"])
        self.assertEqual(p["route"]["open_field_actions"], [])
        self.assertEqual(p["route"]["selector"]["mode"], "numeric")
        self.assertEqual(p["route"]["selector"]["unit"], "V")
        self.assertEqual(p["route"]["selector"]["clear_actions"], [])
        actions = build_actions(p, 0)
        self.assertEqual([a["keys"][0] for a in actions[:4]], ["End", "ArrowUp", "Enter", "Home"])
        self.assertEqual(actions[4], {"op": "type", "text": "0.900"})
        self.assertEqual(actions[5]["keys"], ["Enter"])

    def test_slot_maps_to_absolute_position_from_home(self):
        p = calibrated()
        actions = build_actions(p, 1)
        home = next(i for i, a in enumerate(actions) if a.get("keys") == ["Home"])
        self.assertEqual(actions[home + 1], {"op": "key", "keys": ["ArrowDown"], "count": 12})
        self.assertEqual(expected_changes(p, 1)[0]["to_mv"], 950)
        self.assertEqual(build_actions(p, 1), actions)

    def test_no_confirmation_after_f10(self):
        actions = build_actions(calibrated(), 0)
        f10 = next(i for i, a in enumerate(actions) if a.get("keys") == ["F10"])
        self.assertEqual(actions[f10 + 1], {"op": "wait", "ms": 2000})
        setting_capture = next(i for i, a in enumerate(actions) if a.get("label") == "setting_before_f10")
        self.assertEqual(actions[setting_capture - 1], {"op": "wait", "ms": 2000})
        self.assertFalse(any(a["op"] in ("key", "type") for a in actions[f10 + 1:]))
        self.assertEqual(actions[-1]["phase"], "awaiting_f10_review")

    def test_voltage_range_and_slot_bounds(self):
        for slot in (-1, 5, True):
            with self.assertRaises(ProfileError):
                build_actions(calibrated(), slot)
        p = profile()
        p["voltage_slots_mv"][0] = 895
        with self.assertRaises(ProfileError):
            validate_profile(p)

    def test_entry_key_is_delete_without_total_timeout(self):
        p = profile()
        p["entry"]["key"] = "F12"
        with self.assertRaises(ProfileError):
            validate_profile(p)
        p = profile()
        p["entry"]["total_timeout_s"] = 600
        with self.assertRaises(ProfileError):
            validate_profile(p)

    def test_numeric_entry_has_units(self):
        p = calibrated()
        p["route"]["selector"] = {"mode": "numeric", "unit": "mV", "clear_actions": [{"op": "key", "keys": ["Control", "KeyA"]}]}
        self.assertIn({"op": "type", "text": "900"}, build_actions(p, 0))
        p["route"]["selector"]["unit"] = "V"
        self.assertIn({"op": "type", "text": "0.900"}, build_actions(p, 0))

    def test_calibration_requires_anchor_evidence_and_complete_map(self):
        for mutation in ("evidence", "indices", "home", "route"):
            p = calibrated()
            if mutation == "evidence": p["calibration"]["evidence"] = []
            if mutation == "indices": p["route"]["selector"]["option_indices"][1] = None
            if mutation == "home": p["route"]["selector"]["home_selects_first_option"] = False
            if mutation == "route": p["route"]["navigation_actions"] = []
            with self.assertRaises(ProfileError):
                validate_profile(p)

    def test_review_includes_expected_values_and_profile_binding(self):
        p = calibrated()
        with self.assertRaises(ProfileError):
            review_request(p, 0, {})
        request = review_request(p, 0, {"f10_change_list": {"path": "review.jpg", "sha256": "example"}})
        self.assertEqual(request["expected_changes"][0]["to_mv"], 900)
        self.assertEqual(request["profile_sha256"], profile_sha256(p))
        changed = deepcopy(p)
        changed["route"]["selector"]["option_indices"][0] += 1
        self.assertNotEqual(profile_sha256(p), profile_sha256(changed))

    def test_navigation_cannot_hide_save_keys_or_shell(self):
        for action in ({"op": "key", "keys": ["F10"]}, {"op": "exec", "command": "anything"}):
            p = calibrated()
            p["route"]["navigation_actions"].append(action)
            with self.assertRaises(ProfileError):
                validate_profile(p)

    def test_already_selected_anchor_allows_empty_route(self):
        p = calibrated()
        p["route"]["navigation_actions"] = []
        p["route"]["start_anchor"]["selected_parameter"] = "cldo_vddp_mv"
        validate_profile(p)
        self.assertEqual(build_actions(p, 0)[0]["keys"], ["Enter"])

    def test_anchor_review_can_precede_calibration(self):
        request = anchor_review_request(profile(), {"screen": "bios-anchor.jpg"})
        self.assertEqual(request["phase"], "awaiting_bios_anchor")
        self.assertEqual(request["anchor"]["id"], "from_tweaker_voltage_anchor")

    def test_save_only_profile8_with_authorized_name(self):
        p = profile()
        actions = save_profile_actions(p)
        self.assertFalse(any(a.get("op") == "type" for a in actions))
        self.assertFalse(any(a.get("keys") == ["Backspace"] for a in actions))
        self.assertFalse(any(a.get("keys") == ["F10"] for a in actions))
        self.assertTrue(any(a.get("label") == "profile8_saved" for a in actions))
        for field, value in (("slot", 7), ("name", "test-1")):
            bad = deepcopy(p)
            bad["profile_storage"][field] = value
            with self.assertRaises(ProfileError):
                save_profile_actions(bad)

    def test_profile_name_edit_only_for_explicit_initialization(self):
        p = profile()
        p["profile_storage"]["initialize_name"] = True
        actions = save_profile_actions(p)
        self.assertIn({"op": "type", "text": "agent-tune"}, actions)
        self.assertIn({"op": "key", "keys": ["Backspace"], "count": 32}, actions)
        self.assertFalse(any(a.get("keys") == ["F10"] for a in actions))

    def test_profile_saved_capture_precedes_f10(self):
        actions = build_actions(profile(), 0)
        capture = next(i for i, a in enumerate(actions) if a.get("label") == "profile8_saved")
        f10 = next(i for i, a in enumerate(actions) if a.get("keys") == ["F10"])
        self.assertLess(capture, f10)
        self.assertEqual(actions[-1]["phase"], "awaiting_f10_review")

    def test_save_profile_route_reanchors_both_menus(self):
        actions = save_profile_actions(profile())
        self.assertEqual(actions[0]["keys"], ["Escape"])
        self.assertEqual(actions[1], {"op": "key", "keys": ["ArrowRight"], "count": 4})
        self.assertEqual(actions[2]["keys"], ["End"])
        self.assertEqual(actions[6]["keys"], ["End"])
        self.assertEqual(actions[7], {"op": "key", "keys": ["ArrowUp"], "count": 2})


if __name__ == "__main__":
    unittest.main()
