"""Observed F10-to-HDMI-restoration timing, independent of run state.

HID transmission is not the physical instant that firmware exits Setup. A poll
is an observation interval, not an exact signal edge. Both limits stay explicit.
"""
from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import json
import os
from pathlib import Path
import uuid


def _utc(value):
    if value is None:
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timing timestamps must include a timezone")
    return parsed.astimezone(dt.timezone.utc).isoformat()


def _mono(value):
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("Monotonic timestamps must be nonnegative integer nanoseconds")
    return value


def _seconds(start_utc, start_ns, end_utc, end_ns):
    if start_ns is not None and end_ns is not None and end_ns >= start_ns:
        return (end_ns - start_ns) / 1_000_000_000, "monotonic"
    if start_utc is None or end_utc is None:
        return None, "unavailable"
    return (dt.datetime.fromisoformat(end_utc) - dt.datetime.fromisoformat(start_utc)).total_seconds(), "utc_fallback"


@contextmanager
def _locked(run):
    path = Path(run.path) / "boot-timing.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            if stream.tell() == 0:
                stream.write(b"\0"); stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield path
            finally:
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield path
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _new(run, start_utc, start_ns, ack_utc, ack_ns, origin):
    latency, basis = _seconds(start_utc, start_ns, ack_utc, ack_ns)
    return {
        "schema_version": 1, "run_id": run.name,
        "origin": origin,
        "primary_endpoint": "first_signal_restored",
        "status": "awaiting_signal_absence",
        "start": {
            "command_start_utc": start_utc, "command_start_monotonic_ns": start_ns,
            "ack_utc": ack_utc, "ack_monotonic_ns": ack_ns,
            "command_ack_latency_s": latency, "command_ack_latency_basis": basis,
            "scope": "Recorded F10-confirmation HID send/ack, not measured physical BIOS exit or NVRAM completion",
        },
        "observations": {"count": 0, "first": None, "last": None, "transitions": []},
        "seen_signal_absence": False,
        "endpoints": {"first_signal_lost": None, "first_signal_restored": None, "desktop_ready": None},
        "boot_duration_s": None, "boot_duration_basis": None,
        "limitations": [
            "HDMI availability is supplied by the caller; a returned stale image is not automatically current HDMI evidence.",
            "Restoration requires an observed absence followed by observed valid output.",
            "No POST timeout or boot-failure decision is made by this module.",
            "UTC fallback is sensitive to wall-clock corrections; monotonic observations are preferred.",
        ],
    }


def begin(run, command_start_utc, command_start_monotonic_ns, ack_utc, ack_monotonic_ns):
    """Record one reviewed F10-confirmation command without replacing its origin."""
    start_utc, ack_utc = _utc(command_start_utc), _utc(ack_utc)
    start_ns, ack_ns = _mono(command_start_monotonic_ns), _mono(ack_monotonic_ns)
    if start_utc is None:
        raise ValueError("F10 command timestamp is required")
    if start_ns is not None and ack_ns is not None and ack_ns < start_ns:
        raise ValueError("F10 acknowledgment precedes command on the monotonic clock")
    value = _new(run, start_utc, start_ns, ack_utc, ack_ns, "f10_hid_command")
    with _locked(run) as path:
        if path.exists():
            old = _read(path)
            if old["start"] != value["start"] or old["origin"] != value["origin"]:
                raise FileExistsError("This run already has a different F10 timing origin")
            return old
        _write(path, value)
    return value


def adopt_legacy(run):
    """Use only an existing save_sent_utc; do not invent a monotonic t0 or ack."""
    with _locked(run) as path:
        if path.exists():
            return _read(path)
        stamp = run.load().get("save_sent_utc")
        if not stamp:
            return None
        value = _new(run, _utc(stamp), None, None, None, "legacy_save_sent_utc")
        value["limitations"].append("Legacy state provides only save_sent_utc; no send/ack monotonic timestamps were recorded.")
        _write(path, value)
        return value


def _ensure(run):
    if not (Path(run.path) / "boot-timing.json").exists() and adopt_legacy(run) is None:
        raise RuntimeError("No recorded F10 timing origin")


def _endpoint(value, observation, previous=None):
    start = value["start"]
    t0, ns0 = start["command_start_utc"], start["command_start_monotonic_ns"]
    early, basis_a = _seconds(t0, ns0, observation["start_utc"], observation["monotonic_begin_ns"])
    late, basis_b = _seconds(t0, ns0, observation["end_utc"], observation["monotonic_end_ns"])
    result = {
        "observed_interval_start_utc": observation["start_utc"],
        "observed_interval_end_utc": observation["end_utc"],
        "observed_interval_start_elapsed_s": early,
        "observed_interval_end_elapsed_s": late,
        "elapsed_s": late,
        "basis": basis_b if basis_a == basis_b else "mixed_clocks",
        "scope": "Elapsed until this endpoint was observed; includes HID/firmware/polling delays",
        "physical_transition_elapsed_lower_s": None,
        "physical_transition_elapsed_upper_s": late,
        "physical_bound_basis": None,
        "detection_lag_upper_bound_s": None,
        "previous_observation": previous,
        "observation": observation,
    }
    if previous:
        lower, lower_basis = _seconds(t0, ns0, previous["start_utc"], previous["monotonic_begin_ns"])
        lag, lag_basis = _seconds(previous["start_utc"], previous["monotonic_begin_ns"], observation["end_utc"], observation["monotonic_end_ns"])
        if lower is not None and late is not None and late >= lower:
            result["physical_transition_elapsed_lower_s"] = lower
            result["physical_bound_basis"] = basis_b if lower_basis == basis_b else "mixed_clocks"
        if lag is not None and lag >= 0:
            result["detection_lag_upper_bound_s"] = lag
            result["detection_lag_bound_basis"] = lag_basis
    if late is not None and late < 0:
        result["clock_anomaly"] = "Negative elapsed under UTC fallback; no physical duration is established"
        result["raw_elapsed_s"] = late
        result["elapsed_s"] = None
        result["physical_transition_elapsed_lower_s"] = None
        result["physical_transition_elapsed_upper_s"] = None
        result["physical_bound_basis"] = None
    return result


def observe(run, available, observation_start_utc, observation_end_utc,
            monotonic_begin_ns=None, monotonic_end_ns=None, metadata=None):
    """Record availability edges. A primary restore needs a prior absent poll."""
    if type(available) is not bool:
        raise ValueError("available must be a boolean from the video observer")
    first_utc, last_utc = _utc(observation_start_utc), _utc(observation_end_utc)
    first_ns, last_ns = _mono(monotonic_begin_ns), _mono(monotonic_end_ns)
    if first_utc is None or last_utc is None:
        raise ValueError("Both observation interval timestamps are required")
    if first_ns is not None and last_ns is not None and last_ns < first_ns:
        raise ValueError("Observation monotonic interval is reversed")
    item = {"available": available, "start_utc": first_utc, "end_utc": last_utc,
            "monotonic_begin_ns": first_ns, "monotonic_end_ns": last_ns,
            "metadata": metadata or {}}
    _ensure(run)
    edge = None
    with _locked(run) as path:
        value = _read(path)
        observations = value["observations"]
        previous = observations["last"]
        observations["count"] += 1
        if observations["first"] is None:
            observations["first"] = item
        if previous is None or previous["available"] != available:
            edge = ("initial_presence" if available else "initial_absence") if previous is None else ("signal_restored" if available else "signal_lost")
            event = {"event": edge, **_endpoint(value, item, previous)}
            observations["transitions"].append(event)
            if not available and value["endpoints"]["first_signal_lost"] is None:
                value["endpoints"]["first_signal_lost"] = event
            if available and value["seen_signal_absence"] and value["endpoints"]["first_signal_restored"] is None:
                value["endpoints"]["first_signal_restored"] = event
                value["boot_duration_s"] = event["elapsed_s"]
                value["boot_duration_basis"] = event["basis"]
                value["status"] = "restore_observed"
        if not available:
            value["seen_signal_absence"] = True
            if value["endpoints"]["first_signal_restored"] is None:
                value["status"] = "awaiting_signal_restoration"
        observations["last"] = item
        _write(path, value)
    if edge and callable(getattr(run, "event", None)):
        run.event("boot_video_edge", edge=edge, available=available)
    return value


def desktop_ready(run, observed_utc, observed_monotonic_ns=None, metadata=None):
    """A separate observed desktop-ready marker; never substitutes for HDMI."""
    stamp, ns = _utc(observed_utc), _mono(observed_monotonic_ns)
    if stamp is None:
        raise ValueError("Desktop observation timestamp is required")
    item = {"available": True, "start_utc": stamp, "end_utc": stamp,
            "monotonic_begin_ns": ns, "monotonic_end_ns": ns, "metadata": metadata or {}}
    _ensure(run)
    with _locked(run) as path:
        value = _read(path)
        if value["endpoints"]["desktop_ready"] is None:
            value["endpoints"]["desktop_ready"] = _endpoint(value, item)
            _write(path, value)
        return value
