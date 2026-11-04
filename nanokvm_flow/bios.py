"""Deterministic BIOS action planning. This module never touches hardware.

The profile is a measured keyboard route, not a claim that menu indices can be
inferred from IFR. In particular, a candidate slot is mapped to an absolute BIOS
option index; it is never an increment from the previously selected voltage.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any


class ProfileError(ValueError):
    """A profile is invalid or has not been calibrated for execution."""


NAVIGATION_KEYS = {
    "Delete", "F2", "Escape", "Enter", "Home", "End", "PageUp", "PageDown",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Tab", "Shift", "Control",
    "KeyA", "ControlLeft", "ShiftLeft", "Backspace",
}


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProfileError(f"{label} must be an integer in {minimum}..{maximum}")
    return value


def _actions(actions: Any, label: str) -> None:
    if not isinstance(actions, list):
        raise ProfileError(f"{label} must be a list")
    for action in actions:
        if not isinstance(action, dict):
            raise ProfileError(f"{label} contains a non-object")
        op = action.get("op")
        if op == "key":
            keys = action.get("keys")
            if not isinstance(keys, list) or not keys or any(k not in NAVIGATION_KEYS for k in keys):
                raise ProfileError("Unrecognized navigation key; F10 belongs only to the review boundary")
            _integer(action.get("count", 1), "key count", 1, 200)
        elif op == "wait":
            _integer(action.get("ms"), "wait ms", 0, 30000)
        elif op == "capture":
            if not isinstance(action.get("label"), str) or not action["label"]:
                raise ProfileError("Capture label is required")
        else:
            raise ProfileError(f"Unsupported navigation action: {op}")


def validate_profile(profile: dict[str, Any]) -> None:
    """Validate structure. Uncalibrated profiles can be stored but not executed."""
    if not isinstance(profile, dict) or profile.get("schema_version") != 1:
        raise ProfileError("Expected BIOS profile schema_version 1")
    entry = profile.get("entry", {})
    if entry.get("key") != "Delete":
        raise ProfileError("This board enters Setup with Delete, not F12")
    _integer(entry.get("interval_ms"), "entry interval_ms", 100, 2000)
    if entry.get("total_timeout_s") is not None:
        raise ProfileError("No fixed total POST timeout is configured")
    route = profile.get("route", {})
    if route.get("page") != "Tweaker" or route.get("parameter") != "cldo_vddp_mv":
        raise ProfileError("This profile covers only Tweaker CLDO VDDP")
    anchor = route.get("start_anchor", {})
    if anchor.get("id") != "from_tweaker_voltage_anchor":
        raise ProfileError("The expected start anchor is the profile's measured Tweaker route")
    _actions(route.get("navigation_actions"), "navigation_actions")
    _actions(route.get("open_field_actions"), "open_field_actions")
    slots = profile.get("voltage_slots_mv")
    if not isinstance(slots, list) or not slots or len(set(slots)) != len(slots):
        raise ProfileError("Unique voltage_slots_mv are required")
    for mv in slots:
        _integer(mv, "VDDP mV", 900, 1100)
        if mv % 5:
            raise ProfileError("Tweaker static IFR specifies 5mV steps")
    selector = route.get("selector", {})
    if selector.get("mode") == "list_index":
        indices = selector.get("option_indices")
        if not isinstance(indices, list) or len(indices) != len(slots):
            raise ProfileError("Each candidate needs its measured option index")
        for index in indices:
            if index is not None:
                _integer(index, "option index", 0, 1000)
    elif selector.get("mode") == "numeric":
        if selector.get("unit") not in ("mV", "V"):
            raise ProfileError("Numeric entry requires explicit mV or V units")
        _actions(selector.get("clear_actions", []), "clear_actions")
    else:
        raise ProfileError("selector mode must be list_index or numeric")
    calibration = profile.get("calibration", {})
    if type(calibration.get("verified")) is not bool:
        raise ProfileError("calibration.verified must be explicit")
    if calibration["verified"]:
        if not route["navigation_actions"] and anchor.get("selected_parameter") != "cldo_vddp_mv":
            raise ProfileError("An empty route requires the VDDP field already selected at the verified anchor")
        if not calibration.get("evidence"):
            raise ProfileError("Calibration must reference screen/path evidence")
        if selector["mode"] == "list_index":
            if selector.get("home_selects_first_option") is not True:
                raise ProfileError("Home anchor behavior must be measured")
            if any(i is None for i in selector["option_indices"]):
                raise ProfileError("Calibrated slot mappings must be complete")
    if "profile_storage" in profile:
        storage = profile["profile_storage"]
        if storage.get("slot") != 8 or storage.get("name") != "agent-tune":
            raise ProfileError("Only BIOS Profile8 named agent-tune may be overwritten")
        if profile.get("save_before_f10") is not True:
            raise ProfileError("Profile8 must be saved before F10")


def profile_sha256(profile: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def anchor_review_request(profile: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    """Ask the agent to identify the starting screen before any menu macro."""
    validate_profile(profile)
    return {
        "phase": "awaiting_bios_anchor",
        "anchor": deepcopy(profile["route"]["start_anchor"]),
        "profile_sha256": profile_sha256(profile),
        "evidence": deepcopy(evidence),
        "instructions": [
            "Confirm BIOS Setup, Advanced mode and the exact profile start-anchor page/selected row.",
            "Use the measured relative route; do not start a root-page macro inside a submenu.",
            "Do not accept HDMI presence or a POST logo as the BIOS anchor.",
        ],
    }


def expected_changes(profile: dict[str, Any], slot: int) -> list[dict[str, Any]]:
    validate_profile(profile)
    _integer(slot, "slot", 0, len(profile["voltage_slots_mv"]) - 1)
    mv = profile["voltage_slots_mv"][slot]
    return [{
        "parameter": "cldo_vddp_mv",
        "bios_label": profile["route"]["bios_label"],
        "to_mv": mv,
        "to_text": f"{mv / 1000:.3f}V",
    }]


def save_profile_actions(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Save Profile8/agent-tune from the VDDP submenu, then return to Save & Exit.

    The current parameter edits are preserved. This never opens Load Profiles or
    confirms Save & Exit. The final image shows the named slot in the Save list.
    """
    validate_profile(profile)
    if not profile.get("save_before_f10") or not profile.get("profile_storage"):
        return []  # Earlier archived run profiles did not request profile storage.
    def key(name: str, count: int = 1) -> dict[str, Any]:
        return {"op": "key", "keys": [name], "count": count}
    actions = [
        key("Escape"),  # VDDP submenu -> Tweaker root.
        key("ArrowRight", 4),  # Settings, System Info, Boot, Save & Exit.
        key("End"), key("ArrowUp"), key("Enter"),  # Save Profiles.
        {"op": "wait", "ms": 2000},
        key("End"), key("ArrowUp", 2), key("Enter"),  # Exit -> file -> Profile8.
        {"op": "wait", "ms": 500},
    ]
    if profile["profile_storage"].get("initialize_name") is True:
        # Only first-time initialization or an explicitly reviewed name repair.
        actions += [key("End"), key("Backspace", 32),
                    {"op": "type", "text": profile["profile_storage"]["name"]}]
    actions += [
        key("Enter"),
        {"op": "wait", "ms": 2000},
        {"op": "capture", "label": "profile8_saved_prompt"},
        key("Enter"),  # Dismiss Profile Saved.
        {"op": "wait", "ms": 500},
        key("End"), key("ArrowUp"), key("Enter"),  # Reopen Save Profiles.
        {"op": "wait", "ms": 2000},
        {"op": "capture", "label": "profile8_saved"},
        key("Escape"),
        {"op": "wait", "ms": 500},
    ]
    return actions


def build_actions(profile: dict[str, Any], slot: int, *, allow_uncalibrated: bool = False) -> list[dict[str, Any]]:
    """Plan from a confirmed Setup screen, ending at the F10 review pause.

    Entry-key repetition is a separate runtime stage. allow_uncalibrated is for
    plan inspection only: unresolved routes return a calibration checkpoint and
    cannot emit navigation or F10 key presses.
    """
    changes = expected_changes(profile, slot)
    if not profile["calibration"]["verified"]:
        if not allow_uncalibrated:
            raise ProfileError("BIOS route/field/slot mapping requires one-time screen calibration")
        return [{"op": "checkpoint", "phase": "needs_calibration", "expected_changes": changes}]
    route = profile["route"]
    actions = deepcopy(route["navigation_actions"] + route["open_field_actions"])
    selector = route["selector"]
    if selector["mode"] == "list_index":
        actions.append({"op": "key", "keys": ["Home"], "count": 1})
        count = selector["option_indices"][slot]
        if count:
            actions.append({"op": "key", "keys": ["ArrowDown"], "count": count})
        actions.append({"op": "key", "keys": ["Enter"], "count": 1})
    else:
        actions += deepcopy(selector.get("clear_actions", []))
        mv = changes[0]["to_mv"]
        actions += [
            {"op": "type", "text": str(mv) if selector["unit"] == "mV" else f"{mv / 1000:.3f}"},
            {"op": "key", "keys": ["Enter"], "count": 1},
        ]
    actions += [
        {"op": "wait", "ms": 2000},
        {"op": "capture", "label": "setting_before_f10"},
    ]
    actions += save_profile_actions(profile)
    actions += [
        {"op": "key", "keys": ["F10"], "count": 1},
        {"op": "wait", "ms": 2000},
        {"op": "capture", "label": "f10_change_list"},
        {"op": "checkpoint", "phase": "awaiting_f10_review", "expected_changes": changes},
    ]
    return actions


def review_request(profile: dict[str, Any], slot: int, evidence: dict[str, Any]) -> dict[str, Any]:
    """Data for the agent review; approval and Enter are the host's responsibility."""
    validate_profile(profile)
    if not profile["calibration"]["verified"]:
        raise ProfileError("An uncalibrated profile cannot request save approval")
    if not evidence.get("f10_change_list"):
        raise ProfileError("The F10 change-list screenshot is required")
    return {
        "phase": "awaiting_f10_review",
        "profile_id": profile["id"],
        "profile_sha256": profile_sha256(profile),
        "slot": slot,
        "expected_changes": expected_changes(profile, slot),
        "evidence": deepcopy(evidence),
        "instructions": [
            "Verify the entire F10 change list, including scrollable rows.",
            "Only the expected change may be present; reject unexpected or unreadable rows.",
            "Verify the setting screenshot and intended voltage before approving.",
            "When profile storage is enabled, verify Profile8 is named agent-tune in the saved-slot evidence.",
            "Approval authorizes only this captured review request, not the next candidate.",
        ],
    }
