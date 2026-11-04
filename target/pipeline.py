"""Logged-in target capture; tests require a host SHA-256 receipt.

No reboot, BIOS write, deployment or hardware access occurs at import.
"""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import desktop_capture as desktop

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def load_config(path):
    config = read_json(path)
    if config.get("schema_version") != 1:
        raise ValueError("Target config schema_version must be 1")
    return config


def run_folder(config, run_id):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", run_id) or ".." in run_id:
        raise ValueError("Invalid run_id")
    return Path(os.path.expandvars(config["output_root"])) / run_id


def ps_json(script):
    output = desktop.powershell(script)
    return json.loads(output or "null")


def process_ids(name):
    if not re.fullmatch(r"[A-Za-z0-9_. ~-]+\.exe", name):
        raise ValueError("Invalid executable basename")
    rows = ps_json("@((Get-CimInstance Win32_Process | Where-Object { $_.Name -eq '" +
                   name + "' }).ProcessId) | ConvertTo-Json -Compress")
    return [] if rows is None else rows if isinstance(rows, list) else [rows]


def alive(pid):
    return bool(ps_json(f"[bool](Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue) | ConvertTo-Json"))


def test_processes(config):
    found = {}
    for name in config.get("test_processes", ["TM5.exe", "y-cruncher.exe", "24-ZN5 ~ Komari.exe"]):
        ids = process_ids(name)
        if ids:
            found[name] = ids
    return found


def assert_target(config):
    import winreg
    def get(key, name):
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
            return winreg.QueryValueEx(handle, name)[0]
    cpu = get(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString")
    board = get(r"HARDWARE\DESCRIPTION\System\BIOS", "BaseBoardProduct")
    if config["identity"]["cpu_contains"] not in cpu or board != config["identity"]["board"]:
        raise RuntimeError("Target identity mismatch")
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("Run elevated in the logged-in interactive desktop")
    return {"cpu": cpu, "board": board}


def trace_settings(config):
    root = Path(config["hwtrace_root"])
    # The verified existing receiver command remains in memory; never log it.
    native = read_json(root / "collector.json")
    executable = root / native.get("executable", "build/hwtrace.exe")
    filter_path = root / native["filter"]
    spec = str(native.get("process", "TM5.exe,24-ZN5 ~ Komari.exe"))
    if not executable.is_file() or not filter_path.is_file():
        raise FileNotFoundError("Existing HWTrace executable or filter is missing")
    return native, executable, filter_path, spec


def launch_trace(config, folder):
    native, executable, filter_path, spec = trace_settings(config)
    if process_ids(executable.name):
        raise RuntimeError("A HWTrace collector already exists; refusing a duplicate")
    command = [str(executable), str(filter_path), str(native["ssh_command"]), spec,
               str(native.get("grace_seconds", 600))]
    started_utc = now()
    logfile = folder / ("hwtrace_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".log")
    with logfile.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(command, cwd=executable.parent.parent, stdout=log,
                                stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
    time.sleep(config.get("trace_start_wait_seconds", 3))
    if proc.poll() is not None:
        raise RuntimeError("HWTrace exited before startup confirmation; inspect hwtrace.log")
    return {"pid": proc.pid, "status": "process_alive", "process_spec": spec,
            "executable": str(executable), "receiver_verified": False,
            "started_utc": started_utc, "log": str(logfile)}


def wait_desktop_ready(folder, run_id):
    """At-logon is not yet proof that the interactive Explorer desktop exists."""
    while True:
        observation=ps_json("""
$me=[Diagnostics.Process]::GetCurrentProcess()
$shell=@(Get-Process -Name explorer -ErrorAction SilentlyContinue | Where-Object {$_.SessionId -eq $me.SessionId})
[pscustomobject]@{session_id=$me.SessionId;explorer_pid=if($shell.Count){$shell[0].Id}else{$null};boot_id=(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')} | ConvertTo-Json -Compress
""")
        if observation['session_id']>0 and observation.get('explorer_pid'):
            marker={'run_id':run_id,'desktop_ready':True,'ready_utc':now(),**observation}
            write_json(folder/'desktop-ready.json',marker)
            return marker
        time.sleep(.5)


def capture(config_path, run_id, delay=None):
    config = load_config(config_path)
    identity = assert_target(config)
    folder = run_folder(config, run_id)
    complete = folder / "complete.json"
    if complete.is_file():
        return read_json(complete)
    if test_processes(config):
        raise RuntimeError("A stress test is running; capture was not started")
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / "capture.lock"
    # Explicit inspection/removal is required after an interrupted attempt.
    with lock.open("x", encoding="utf-8") as stream:
        stream.write(str(os.getpid()))
    state = {"run_id": run_id, "folder": str(folder), "phase": "delay", "started_utc": now()}
    write_json(folder / "state.json", state)
    try:
        state['phase']='awaiting_desktop'
        write_json(folder/'state.json',state)
        wait_desktop_ready(folder,run_id)
        state['phase']='login_delay'
        write_json(folder/'state.json',state)
        time.sleep(config.get("login_delay_seconds", 10) if delay is None else delay)
        if test_processes(config):
            raise RuntimeError("A stress test started during login delay")
        boot = desktop.powershell("(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')")
        desktop.AFU = Path(config["afu"])
        for path in (desktop.AFU,):
            if not path.is_file():
                raise FileNotFoundError(path)
        state["phase"] = "hwinfo"
        write_json(folder / "state.json", state)
        try:
            hwinfo = desktop.ensure_hwinfo(config, process_ids)
            write_json(folder / "hwinfo.json", hwinfo)
        except desktop.HWiNFOReadyError as error:
            write_json(folder / "hwinfo.json", error.diagnostic)
            raise
        state["phase"] = "zentimings_cli"
        write_json(folder / "state.json", state)
        zen = folder / f"ZenTimings_{run_id}.json"
        dump = folder / f"F12d_{run_id}.bin"
        if zen.exists() or dump.exists():
            raise FileExistsError("Capture destination already exists")
        exporter = desktop.export_zen_cli(config, zen)
        state["phase"] = "dump"
        write_json(folder / "state.json", state)
        with (folder / "afu.log").open("w", encoding="utf-8") as log:
            subprocess.run([str(desktop.AFU), str(dump), "/O"], cwd=desktop.AFU.parent,
                           stdout=log, stderr=subprocess.STDOUT, check=True, creationflags=NO_WINDOW)
        if dump.stat().st_size != 33554432:
            raise RuntimeError("AFU read-only dump is not 32 MiB")
        desktop.validate_zen(zen)
        manifest = {"schema_version": 1, "run_id": run_id, "boot_id": boot,
                    "folder": str(folder), "identity": identity, "status": "complete",
                    "started_utc": state["started_utc"], "completed_utc": now(),
                    "files": [{"name": f.name, "size": f.stat().st_size, "sha256": sha256(f)}
                              for f in (dump, zen)], "hwtrace": launch_trace(config, folder),
                    "hwinfo": hwinfo, "zentimings_export": exporter}
        write_json(folder / "manifest.json", manifest)
        result = {"run_id": run_id, "status": "complete", "folder": str(folder),
                  "manifest_sha256": sha256(folder / "manifest.json")}
        write_json(complete, result)
        state.update(phase="awaiting_host_receipt", completed_utc=now())
        write_json(folder / "state.json", state)
        return result
    except Exception as error:
        state["failure_phase"] = state.get("phase")
        state.update(phase="failed", error=type(error).__name__ + ": " + str(error), failed_utc=now())
        write_json(folder / "failed.json", state)
        write_json(folder / "state.json", state)
        raise


def verify_receipt(folder, run_id):
    manifest = read_json(folder / "manifest.json")
    complete = read_json(folder / "complete.json")
    receipt = read_json(folder / "received.json")
    expected = sha256(folder / "manifest.json")
    if manifest.get("status") != "complete" or complete.get("status") != "complete":
        raise RuntimeError("Capture is not complete")
    if any(x.get("run_id") != run_id for x in (manifest, complete, receipt)):
        raise RuntimeError("Receipt run_id mismatch")
    if complete.get("manifest_sha256") != expected or receipt.get("manifest_sha256") != expected:
        raise RuntimeError("Host receipt does not match the capture manifest")
    if receipt.get("files") != manifest.get("files"):
        raise RuntimeError("Host receipt file list differs")
    if receipt.get("readback_verified") is not True:
        raise RuntimeError("Host BIOS readback review is not verified")
    telemetry = receipt.get("telemetry_verified")
    if (not isinstance(telemetry, dict) or not isinstance(telemetry.get("session"), str)
            or not telemetry["session"] or type(telemetry.get("seq")) is not int
            or telemetry["seq"] < 0 or not isinstance(telemetry.get("timestamp"), str)):
        raise RuntimeError("Host receiver telemetry verification is missing or invalid")
    try:
        timestamp = dt.datetime.fromisoformat(telemetry["timestamp"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("timestamp must have a timezone")
    except ValueError as error:
        raise RuntimeError("Host receiver telemetry timestamp is invalid") from error
    for item in manifest["files"]:
        if Path(item["name"]).name != item["name"]:
            raise RuntimeError("Invalid manifest filename")
        file = folder / item["name"]
        if file.stat().st_size != item["size"] or sha256(file) != item["sha256"]:
            raise RuntimeError("Captured file changed after host receipt")
    return manifest


def start_test(config_path, run_id, kind):
    config = load_config(config_path)
    assert_target(config)
    folder = run_folder(config, run_id)
    manifest = verify_receipt(folder, run_id)
    if test_processes(config):
        raise RuntimeError("A stress test is already running")
    test_state = folder / "test.json"
    if test_state.exists():
        previous = read_json(test_state)
        if previous.get("controller_pid") and alive(previous["controller_pid"]):
            raise RuntimeError("A test controller is still running")
    kind_lock = folder / f"test-{kind}.lock"
    if kind_lock.exists():
        raise RuntimeError("This test kind already has a launch record")
    profile = config["tests"][kind]
    if not Path(profile["executable"]).is_file():
        raise FileNotFoundError(profile["executable"])
    trace = manifest["hwtrace"]
    if trace["pid"] not in process_ids(Path(trace["executable"]).name):
        trace = launch_trace(config, folder)
        manifest["hwtrace"] = trace
        manifest["receiver_recheck_utc"] = now()
        write_json(folder / "manifest.json", manifest)
        complete = read_json(folder / "complete.json")
        complete["manifest_sha256"] = sha256(folder / "manifest.json")
        write_json(folder / "complete.json", complete)
        result = {"run_id": run_id, "kind": kind, "status": "awaiting_receiver",
                  "hwtrace": trace, "manifest_sha256": complete["manifest_sha256"]}
        write_json(folder / "telemetry-recheck.json", result)
        return result
    monitored = {x.strip().casefold() for x in trace["process_spec"].split(",")}
    if not monitored.intersection(x.casefold() for x in profile["monitored_processes"]):
        raise RuntimeError("HWTrace process list does not cover the requested test")
    with kind_lock.open("x", encoding="utf-8") as stream:
        stream.write(now())
    task_name = f"OC-ScriptedTest-{run_id}-{kind}"
    python = Path(sys.executable).with_name("pythonw.exe")
    if not python.is_file():
        python = Path(sys.executable)
    runner = Path(__file__).with_name("test_runner.py")
    args = f'"{runner}" --config "{Path(config_path).resolve()}" --run-id {run_id} --kind {kind}'
    quote = lambda text: "'" + str(text).replace("'", "''") + "'"
    # SSH normally runs in Session 0. Use the native interactive principal so
    # the test appears on the same logged-in desktop as HWiNFO and ZenTimings.
    script = f"""
$ErrorActionPreference='Stop'
$name={quote(task_name)}
if(Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue){{throw 'Test task already exists'}}
$user=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$action=New-ScheduledTaskAction -Execute {quote(python)} -Argument {quote(args)} -WorkingDirectory {quote(folder)}
$principal=New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $name -Action $action -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $name
"""
    desktop.powershell(script)
    result = {"run_id": run_id, "kind": kind, "status": "launch_requested", "task_name": task_name,
              "started_utc": now(), "hwtrace": trace}
    # Do not overwrite a faster runner's already-published start state.
    if not test_state.is_file() or read_json(test_state).get("kind") != kind:
        write_json(test_state, result)
    return result


def status(config_path, run_id):
    config=load_config(config_path)
    folder = run_folder(config, run_id)
    result = {"run_id": run_id, "folder": str(folder), "status": "not_started"}
    for name in ("state.json", "complete.json", "test.json", "test-result.json",
                 "test-vt3-result.json", "test-tm5-result.json"):
        path = folder / name
        if path.is_file():
            result[name.removesuffix(".json")] = read_json(path)
    if (folder / "complete.json").is_file():
        result["status"] = "complete"
    elif (folder / "failed.json").is_file():
        result["status"] = "failed"
    elif folder.exists():
        result["status"] = "capturing"
    test_result=result.get('test-tm5-result',{})
    if test_result.get('status')=='process_started':
        result['test_process_alive']=test_result['pid'] in process_ids(Path(config['tests']['tm5']['executable']).name)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("capture", "capture-pending", "status", "start-test"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--pending", help="Read run_id from host-written JSON for an at-logon task")
    parser.add_argument("--delay", type=float)
    parser.add_argument("--kind", choices=("vt3", "tm5"))
    args = parser.parse_args()
    if args.command == "capture-pending" and (not args.pending or not Path(args.pending).is_file()):
        return
    run_id = args.run_id or (read_json(args.pending).get("run_id") if args.pending else None)
    if not run_id:
        parser.error("--run-id or a pending JSON run_id is required")
    if args.delay is not None and args.delay < 0:
        parser.error("--delay cannot be negative")
    if args.command in ("capture", "capture-pending"):
        result = capture(args.config, run_id, args.delay)
    elif args.command == "start-test":
        if not args.kind:
            parser.error("start-test requires --kind")
        result = start_test(args.config, run_id, args.kind)
    else:
        result = status(args.config, run_id)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
