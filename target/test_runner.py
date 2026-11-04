"""A finite target runner: ten VT3 launches, with independent real timing/logs."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

import pipeline
import vt3_results


def _save(folder, kind, result):
    pipeline.write_json(folder / f"test-{kind}-result.json", result)
    pipeline.write_json(folder / "test-result.json", result)


def run_vt3(folder, run_id, profile, *, popen=None, clock=None, sleep=None):
    """No target access here; Popen/clock/sleep are replaceable offline fakes."""
    folder = Path(folder)
    count = vt3_results.validate_profile(profile)
    popen = subprocess.Popen if popen is None else popen
    clock = vt3_results.Clock() if clock is None else clock
    sleep = time.sleep if sleep is None else sleep
    for name in ([f"vt3_run{i}.log" for i in range(1, count + 1)]
                 + ["test-vt3-summary.json", "test-vt3-summary.csv", "test-vt3-config.json", "test-vt3-artifacts.json"]):
        if (folder / name).exists():
            raise FileExistsError(folder / name)
    rows, gaps = [], []
    error = None
    batch_start = clock.sample()
    for index in range(1, count + 1):
        logfile = folder / f"vt3_run{index}.log"
        arguments = [x.replace("{log}", str(logfile)) for x in profile["arguments"]]
        start = clock.sample()
        try:
            proc = popen([profile["executable"], *arguments], cwd=profile["working_directory"],
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            process_observed = clock.sample()
            active = {"run_id": run_id, "kind": "vt3", "status": "process_started", "index": index,
                      "pid": proc.pid, "controller_pid": os.getpid(), "started_utc": start["utc"],
                      "start_uptime_ms": start["uptime_ms"], "process_start_observed": process_observed,
                      "log": str(logfile), "expected_count": count}
            pipeline.write_json(folder / "test.json", active)
            _save(folder, "vt3", {"run_id": run_id, "kind": "vt3", "status": "running",
                                  "expected_count": count, "completed_count": len(rows), "runs": rows,
                                  "active": active, "gaps": gaps})
            exit_code = proc.wait()
            end = clock.sample()
            row = vt3_results.finish_log(logfile, index, proc.pid, start, process_observed,
                                         end, exit_code, profile)
            row["gap_before_s"] = gaps[-1]["duration_s"] if gaps else 0
            rows.append(row)
            # Algorithm failures continue; program/runner failures are explicit.
            if row["result"] == "program_error" or (exit_code != 0 and row["result"] != "failed"):
                error = {"type": "program_exit", "index": index, "exit_code": exit_code}
                break
            _save(folder, "vt3", {"run_id": run_id, "kind": "vt3", "status": "running",
                                  "expected_count": count, "completed_count": len(rows),
                                  "runs": rows, "gaps": gaps, "active": None})
            if index < count:
                gap_start = clock.sample()
                sleep(profile.get("gap_seconds", 15))
                gap_end = clock.sample()
                gaps.append({"after_index": index, "before_index": index + 1,
                             "requested_seconds": profile.get("gap_seconds", 15),
                             **vt3_results.duration(gap_start, gap_end)})
                row["gap_after_s"] = gaps[-1]["duration_s"]
        except Exception as exc:
            error = {"type": type(exc).__name__, "index": index, "message": str(exc)[:1000]}
            break
    batch_end = clock.sample()
    # The manifest is fully materialized and hashed before finished is published.
    summary, manifest = vt3_results.publish_artifacts(folder, run_id, profile, rows, gaps,
                                                     batch_start, batch_end, error)
    outcome = {"run_id": run_id, "kind": "vt3", "status": "finished" if error is None else "failed",
               "runs": rows, "expected_count": count, "completed_count": len(rows),
               "count_complete": summary["count_complete"], "evidence_complete": summary["evidence_complete"],
               "counts": summary["counts"], "passed": summary["passed"], "gaps": gaps,
               "batch_timing": summary["batch_timing"], "total_run_duration_s": summary["total_run_duration_s"],
               "total_gap_duration_s": summary["total_gap_duration_s"], "error": error,
               "controller_overhead_s": summary["controller_overhead_s"],
               "manifest": manifest, "artifacts_manifest": manifest}
    _save(folder, "vt3", outcome)
    pipeline.write_json(folder / "test.json", {"run_id": run_id, "kind": "vt3", "status": outcome["status"],
                         "controller_pid": os.getpid(), "completed_count": len(rows), "expected_count": count})
    return outcome


def run(config_path, run_id, kind):
    config = pipeline.load_config(config_path)
    pipeline.assert_target(config)
    folder = pipeline.run_folder(config, run_id)
    pipeline.verify_receipt(folder, run_id)
    profile = config["tests"][kind]
    pipeline.write_json(folder / "test.json", {"run_id": run_id, "kind": kind,
                         "status": "controller_running", "controller_pid": os.getpid(),
                         "started_utc": pipeline.now()})
    if kind == "vt3":
        return run_vt3(folder, run_id, profile)
    # Keep TM5's existing behavior and log-offset handoff for actual Testing0 checks.
    tm5_log = Path(profile["working_directory"]) / "Log.txt"
    initial_size = tm5_log.stat().st_size if tm5_log.exists() else 0
    proc = subprocess.Popen([profile["executable"], *profile.get("arguments", [])],
                            cwd=profile["working_directory"])
    result = {"run_id": run_id, "kind": kind, "status": "process_started", "pid": proc.pid,
              "started_utc": pipeline.now(), "log_path": str(tm5_log), "log_initial_size": initial_size}
    _save(folder, kind, result)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--kind", choices=("vt3", "tm5"), required=True)
    a = p.parse_args()
    try:
        result = run(a.config, a.run_id, a.kind)
        if result.get("status") == "failed":
            sys.exit(1)
    except Exception as error:
        folder = pipeline.run_folder(pipeline.load_config(a.config), a.run_id)
        failure = {"run_id": a.run_id, "kind": a.kind, "status": "failed",
                   "error": type(error).__name__ + ": " + str(error), "finished_utc": pipeline.now()}
        _save(folder, a.kind, failure)
        raise
