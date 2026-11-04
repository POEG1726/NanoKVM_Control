"""VT3 log classification and observed launch-to-exit timing artifacts.

``Total Elapsed`` printed by this y-cruncher build is deliberately not used.
It can remain the iteration-start value after the test has run for minutes.
"""
from __future__ import annotations

import csv
import ctypes
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import time


class Clock:
    def sample(self):
        monotonic_ns = time.perf_counter_ns()
        if hasattr(ctypes, "windll"):
            ticks = ctypes.windll.kernel32.GetTickCount64
            ticks.restype = ctypes.c_ulonglong
            uptime_ms = int(ticks())
        else:
            uptime_ms = time.monotonic_ns() // 1_000_000
        return {"utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "uptime_ms": uptime_ms, "monotonic_ns": monotonic_ns}


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def object_sha(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def duration(start, end):
    seconds = (end["monotonic_ns"] - start["monotonic_ns"]) / 1_000_000_000
    if seconds < 0:
        raise RuntimeError("Monotonic clock moved backwards")
    wall = (dt.datetime.fromisoformat(end["utc"]) - dt.datetime.fromisoformat(start["utc"])).total_seconds()
    return {"duration_s": seconds, "wall_duration_s": wall,
            "started_utc": start["utc"], "finished_utc": end["utc"],
            "start_uptime_ms": start["uptime_ms"], "end_uptime_ms": end["uptime_ms"],
            "start_monotonic_ns": start["monotonic_ns"], "end_monotonic_ns": end["monotonic_ns"]}


def validate_profile(profile):
    count = profile.get("repeats", 10)
    expected = profile.get("expected_count", 10)
    if type(count) is not int or type(expected) is not int or count != 10 or expected != count:
        raise ValueError("VT3 batch requires repeats=expected_count=10")
    args = profile.get("arguments", [])
    if ("VT3" not in args or [x for x in args if str(x).startswith("-D:")] != ["-D:600"]
            or [x for x in args if str(x).startswith("-TL:")] != ["-TL:600"]
            or sum(str(x).lower().startswith("logfile:") and "{log}" in str(x) for x in args) != 1):
        raise ValueError("VT3 requires one unique logfile:{log}, -D:600 and -TL:600")
    if not isinstance(profile.get("gap_seconds", 15), (int, float)) or profile.get("gap_seconds", 15) < 0:
        raise ValueError("gap_seconds must be nonnegative")
    return count


def parse_log(text, exit_code, *, present=True):
    exceptions = list(dict.fromkeys(re.findall(r"Exception Encountered:\s*([A-Za-z_][A-Za-z0-9_]*)", text)))
    cores = list(dict.fromkeys(int(x) for x in re.findall(r"Error\(s\) encountered on logical core\s+(\d+)", text)))
    error_types = []
    if "Checksum Mismatch" in text:
        error_types.append("ChecksumMismatch")
    if "Coefficient is too large" in text:
        error_types.append("CoefficientTooLarge")
    stress_exceptions = {"StressTestFailedException", "ConvolutionFailedException", "AlgorithmFailedException"}
    failed = bool(cores or error_types or stress_exceptions.intersection(exceptions)
                  or "Stress test failed" in text or "Running VT3: Failed" in text)
    passed = "Running VT3: Passed" in text and "Test Finished" in text
    if not present:
        result = "unknown"
    elif failed:
        result = "failed"
    elif exit_code != 0 or exceptions:
        result = "program_error"
    elif passed:
        result = "passed"
    else:
        result = "unknown"
    observed = {}
    patterns = {
        "logical_threads": r"^\s*1\s+Logical Cores:\s+(\d+)",
        "memory_gib": r"^\s*2\s+Memory:\s+([\d.]+)\s+GiB",
        "seconds_per_test": r"Time Limit:\s+(\d+)\s+seconds per test",
        "seconds_total": r"seconds per test\s*/\s*(\d+)\s+seconds total",
    }
    for name, pattern in patterns.items():
        found = re.findall(pattern, text, re.MULTILINE)
        if found:
            observed[name] = float(found[-1]) if name == "memory_gib" else int(found[-1])
    allocated = re.findall(r"Core\s+(\d+):\s+[\d.]+\s+(?:GiB|MiB)", text)
    if allocated:
        observed["allocated_logical_cpus"] = sorted(set(map(int, allocated)))
    return {"result": result, "failure_detected_threads": cores, "error_types": error_types,
            "exception_types": exceptions, "observed_config": observed,
            "program_log_present": present,
            "program_total_elapsed_used": False}


def finish_log(logfile, index, pid, start, process_observed, end, exit_code, profile):
    logfile = Path(logfile)
    present = logfile.is_file()
    original = logfile.read_bytes() if present else b""
    parsed = parse_log(original.decode("utf-8-sig", errors="replace"), exit_code, present=present)
    if (parsed["result"] == "passed" and any(parsed["observed_config"].get(k, 600) != 600
                                              for k in ("seconds_per_test", "seconds_total"))):
        parsed["result"] = "unknown"
        parsed["configuration_mismatch"] = True
    row = {"index": index, "pid": pid, "exit_code": exit_code, **parsed,
           **duration(start, end), "process_start_observed": process_observed,
           "duration_basis": "Popen invocation to wait return; includes startup/allocation/exit, not Testing 0",
           "log": str(logfile), "configuration": profile,
           "command": [profile["executable"], *[x.replace("{log}", str(logfile)) for x in profile["arguments"]]],
           "gap_before_s": 0, "gap_after_s": 0, "duration_excludes_gap": True,
           "profile_sha256": object_sha(profile),
           "program_log_sha256": hashlib.sha256(original).hexdigest() if present else None}
    marker = {"index": index, "pid": pid, "start": start, "process_start_observed": process_observed,
              "end": end, "exit_code": exit_code, "duration_s": row["duration_s"],
              "wall_duration_s": row["wall_duration_s"], "duration_basis": row["duration_basis"],
              "program_log_present": present, "program_log_sha256": row["program_log_sha256"],
              "configuration": profile, "profile_sha256": row["profile_sha256"]}
    # Preserve every original program byte; append runner metadata only after exit.
    with logfile.open("ab") as stream:
        stream.write(b"\r\n[Runner] " + json.dumps(marker, ensure_ascii=True, sort_keys=True).encode() + b"\r\n")
    row["sha256"] = digest(logfile)
    return row


def publish_artifacts(folder, run_id, profile, rows, gaps, batch_start, batch_end, error=None):
    folder = Path(folder)
    expected = profile.get("expected_count", 10)
    count_complete = (len(rows) == expected and [r["index"] for r in rows] == list(range(1, expected + 1)))
    batch_timing = duration(batch_start, batch_end)
    total_runs = sum(r["duration_s"] for r in rows)
    total_gaps = sum(g["duration_s"] for g in gaps)
    summary = {"schema_version": 1, "run_id": run_id, "kind": "vt3",
               "expected_count": expected, "completed_count": len(rows), "run_count": len(rows),
               "count_complete": count_complete,
               "evidence_complete": count_complete and all(r["program_log_present"] for r in rows),
               "passed": count_complete and not error and all(r["result"] == "passed" for r in rows),
               "counts": {name: sum(r["result"] == name for r in rows)
                          for name in ("passed", "failed", "unknown", "program_error")},
               "profile_sha256": object_sha(profile), "configuration": profile,
               "batch_timing": batch_timing,
               "total_run_duration_s": total_runs, "total_gap_duration_s": total_gaps,
               "controller_overhead_s": batch_timing["duration_s"] - total_runs - total_gaps,
               "gaps": gaps, "runs": rows, "error": error}
    jsonfile = folder / "test-vt3-summary.json"
    configfile = folder / "test-vt3-config.json"
    csvfile = folder / "test-vt3-summary.csv"
    for file, obj in ((jsonfile, summary), (configfile, profile)):
        with file.open("x", encoding="utf-8") as stream:
            json.dump(obj, stream, indent=2, ensure_ascii=False)
    fields = ["index", "result", "pid", "exit_code", "started_utc", "finished_utc", "start_uptime_ms",
              "end_uptime_ms", "duration_s", "wall_duration_s", "start_monotonic_ns", "end_monotonic_ns",
              "gap_before_s", "gap_after_s", "failure_detected_threads", "error_types", "exception_types", "program_log_present",
              "program_total_elapsed_used", "profile_sha256", "log", "sha256"]
    with csvfile.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(row[k], ensure_ascii=False) if isinstance(row[k], (list, dict)) else row[k]
                             for k in fields})
    files = []
    for path, role in ([(folder / Path(r["log"]).name, "log") for r in rows]
                       + [(jsonfile, "summary_json"), (csvfile, "summary_csv"), (configfile, "config_json")]):
        if path.parent.resolve() != folder.resolve() or not path.is_file():
            raise RuntimeError("Artifact must be a file directly inside this run folder")
        files.append({"name": path.name, "role": role, "size": path.stat().st_size, "sha256": digest(path)})
    manifest = {"schema_version": 1, "run_id": run_id, "kind": "vt3",
                "status": "complete" if count_complete and error is None else "partial",
                "expected_count": expected, "completed_count": len(rows), "run_count": len(rows),
                "profile_sha256": object_sha(profile), "files": files}
    manifestfile = folder / "test-vt3-artifacts.json"
    with manifestfile.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, ensure_ascii=False)
    return summary, {"name": manifestfile.name, "size": manifestfile.stat().st_size,
                     "sha256": digest(manifestfile)}
