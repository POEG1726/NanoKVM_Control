"""Read-only local evidence availability for queued result messages.

These observations never control BIOS actions, sampling, or stress tests.
"""
from __future__ import annotations

import datetime as dt
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _checked(check):
    try:
        return check()
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error) as error:
        return {"available": False, "path": None, "reason": "check_failed", "error_type": type(error).__name__}


def _export(folder, extension):
    manifest_path = folder / "target" / "manifest.json"
    if not manifest_path.is_file():
        return {"available": False, "path": None, "reason": "local_manifest_missing"}
    entries = [item for item in _json(manifest_path)["files"] if item["name"].endswith(extension)]
    if len(entries) != 1:
        return {"available": False, "path": None, "reason": "artifact_not_identified"}
    item = entries[0]
    name = item["name"]
    if Path(name).name != name or "/" in name or "\\" in name:
        return {"available": False, "path": None, "reason": "invalid_artifact_basename"}
    path = manifest_path.parent / name
    result = {"available": False, "path": str(path.resolve())}
    if not path.is_file():
        return dict(result, reason="local_file_missing")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if path.stat().st_size != item["size"] or digest != item["sha256"]:
        return dict(result, reason="manifest_size_or_hash_mismatch")
    return dict(result, available=True, reason="matches_local_manifest", size=item["size"], sha256=digest)


def _same_f10(left, right):
    def timestamp(value):
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return bool(left.get("save_sent_utc") and right.get("save_sent_utc")
                and timestamp(left["save_sent_utc"]) == timestamp(right["save_sent_utc"]))


def _dsview(folder, state):
    source = folder
    if not (source / "dsview-start.json").is_file():
        parent = state.get("parent_run_id", "")
        if (isinstance(parent, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", parent)
                and ".." not in parent):
            candidate = folder.parent / parent
            parent_state = candidate / "state.json"
            if parent_state.is_file() and _same_f10(state, _json(parent_state)):
                source = candidate
    start_path, stop_path = source / "dsview-start.json", source / "dsview-stop.json"
    if not start_path.is_file():
        return {"available": False, "path": None, "reason": "no_capture_linked_to_this_boot"}
    if not stop_path.is_file():
        return {"available": False, "path": None, "reason": "capture_not_stopped_and_flushed",
                "start_receipt": str(start_path.resolve())}
    start, stop = _json(start_path), _json(stop_path)
    status = stop["state"]
    output = status.get("output_directory")
    if not output:
        return {"available": False, "path": None, "reason": "capture_directory_missing"}
    path = Path(output)
    result = {"available": False, "path": str(path.resolve()), "source_run_id": source.name,
              "start_receipt": str(start_path.resolve()), "stop_receipt": str(stop_path.resolve())}
    expected = start.get("state", {}).get("output_directory") or start.get("output_directory")
    if not expected or not path.resolve().is_relative_to(Path(expected).resolve()):
        return dict(result, reason="capture_receipts_do_not_match")
    if any(part.lower() in ("secrets", ".env") for part in path.parts):
        return dict(result, reason="restricted_capture_path")
    if (stop.get("status") != "stopped_and_flushed" or status.get("flushed") is not True
            or status.get("recorder_failed") is not False or status.get("queued_bytes") != 0
            or status.get("recorder_running") is not False or status.get("working") is not False):
        return dict(result, reason="capture_not_stopped_and_flushed")
    if not path.is_dir():
        return dict(result, reason="capture_directory_missing")
    files = [item for item in path.iterdir() if item.is_file()]
    if any(item.name.endswith(".part") for item in files):
        return dict(result, reason="partial_capture_files_remain")
    if not any(item.stat().st_size for item in files):
        return dict(result, reason="capture_files_missing_or_empty")
    return dict(result, available=True, reason="linked_capture_stopped_flushed_and_on_disk",
                file_count=len(files), format=status.get("format"),
                samples_written=status.get("samples_written"), scope="stored_capture_not_a_signal_quality_verdict")


def _hwtrace(config, state):
    database, session = config.get("telemetry_db"), (state.get("telemetry") or {}).get("session")
    result = {"available": False, "path": str(Path(database).resolve()) if database else None,
              "session": session, "scope": "recorded_samples_not_a_live_process_claim"}
    if not database or not Path(database).is_file():
        return dict(result, reason="local_telemetry_database_missing")
    if not session:
        return dict(result, reason="run_telemetry_session_missing")
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
        connection.execute("pragma query_only=on")
        count, first, last = connection.execute(
            "select count(*),min(timestamp),max(timestamp) from records where session=? and kind='sample'",
            (session,)).fetchone()
    return dict(result, available=count > 0, reason="recorded_session_samples" if count else "session_has_no_samples",
                sample_count=count, first_sample_utc=first, last_sample_utc=last)


def data_sources(config, folder, state):
    folder = Path(folder)
    return {"dump": _checked(lambda: _export(folder, ".bin")),
            "zentimings": _checked(lambda: _export(folder, ".json")),
            "dsview_capture": _checked(lambda: _dsview(folder, state)),
            "hwtrace": _checked(lambda: _hwtrace(config, state))}


def availability_summary(sources):
    names = {"dump": "dump", "zentimings": "ZenTimings", "dsview_capture": "DSView capture", "hwtrace": "HWTrace"}
    return "数据：" + "；".join(name + ("可用" if sources[key]["available"] else "缺失或未验证")
                               for key, name in names.items()) + "。"
