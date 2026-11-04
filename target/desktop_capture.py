"""HWiNFO readiness and one-shot ZenTimings CLI export helpers.
UIAutomation is used only when HWiNFO is absent and needs startup.
ZenTimings export never constructs or manipulates a GUI.
"""
import argparse
import ctypes as C
from ctypes import wintypes as W
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import struct

UI_HELPER = Path(__file__).with_name("boot_capture_find_control.ps1")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def powershell(script):
    import base64
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.check_output(
        ["pwsh", "-NoProfile", "-OutputFormat", "Text", "-EncodedCommand", encoded],
        encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW).strip()


def validate_zen(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise RuntimeError("ZenTimings export must be a JSON object")
    # Required sections from the complete 2026-11-03 13:25 target export.
    for group, sections in {
        "static": ("system", "memory", "spd"),
        "config": ("timings", "aod", "apob"),
        "readings": ("power_table", "sensors", "dimm"),
    }.items():
        if not isinstance(data.get(group), dict):
            raise RuntimeError(f"Missing ZenTimings group: {group}")
        for section in sections:
            if section not in data[group] or data[group][section] is None:
                raise RuntimeError(f"Missing ZenTimings section: {group}.{section}")
    return data


class Desktop:
    def __init__(self):
        self.u = C.WinDLL("user32", use_last_error=True)
        self.k = C.WinDLL("kernel32", use_last_error=True)
        self.u.SetProcessDPIAware()
        self.callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        self.u.EnumWindows.argtypes = [self.callback, W.LPARAM]
        self.u.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
        self.u.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
        self.u.SetForegroundWindow.argtypes = [W.HWND]
        self.u.GetForegroundWindow.restype = W.HWND
        self.u.ShowWindow.argtypes = [W.HWND, C.c_int]
        self.u.SetWindowPos.argtypes = [W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT]
        self.k.OpenFileMappingW.argtypes = [W.DWORD, W.BOOL, W.LPCWSTR]
        self.k.OpenFileMappingW.restype = W.HANDLE
        self.k.CloseHandle.argtypes = [W.HANDLE]

    def windows(self):
        found = []
        @self.callback
        def visit(hwnd, _):
            text = C.create_unicode_buffer(512)
            self.u.GetWindowTextW(hwnd, text, len(text))
            if text.value:
                pid = W.DWORD()
                self.u.GetWindowThreadProcessId(hwnd, C.byref(pid))
                found.append((hwnd, text.value, pid.value))
            return True
        self.u.EnumWindows(visit, 0)
        return found

    def find(self, pattern, process_id=None):
        return next((w for w in self.windows() if re.search(pattern, w[1])
                     and (process_id is None or w[2] == process_id)), None)

    def wait_window(self, pattern, process_id=None, seconds=60):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            window = self.find(pattern, process_id)
            if window:
                return window
            time.sleep(0.5)
        raise RuntimeError(f"Window did not appear: {pattern}")

    def focus(self, hwnd):
        self.u.ShowWindow(hwnd, 9)
        self.key(0x12)
        self.u.SetForegroundWindow(hwnd)
        self.u.SetWindowPos(hwnd, None, 100, 100, 0, 0, 0x0015)
        time.sleep(0.3)
        if self.u.GetForegroundWindow() != hwnd:
            raise RuntimeError("Could not activate application window")

    def key(self, *keys):
        for key in keys:
            self.u.keybd_event(key, 0, 0, 0)
        for key in reversed(keys):
            self.u.keybd_event(key, 0, 2, 0)
        time.sleep(0.1)

    def click(self, x, y):
        self.u.SetCursorPos(int(x), int(y))
        self.u.mouse_event(2, 0, 0, 0, 0)
        self.u.mouse_event(4, 0, 0, 0, 0)
        time.sleep(0.4)

    def control(self, process_id, pattern, kind="", value=None):
        command = ["powershell.exe", "-NoProfile", "-File", str(UI_HELPER),
                   "-TargetProcessId", str(process_id), "-NamePattern", pattern]
        if kind:
            command += ["-Kind", kind]
        if value is not None:
            command += ["-Value", value]
        result = subprocess.run(command, capture_output=True, encoding="utf-8",
                                timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError(f"Control not found: {pattern}; {result.stderr[-400:]}")
        control = json.loads(result.stdout.lstrip("\ufeff"))
        if value is None:
            self.click(control["x"], control["y"])

    def shared_memory(self):
        return probe_hwinfo_shared_memory()["ready"]


def validate_sm2_header(header, region_size):
    """Use the same bounds/layout checks as the existing HWTrace collector."""
    if len(header) < 48 or not 48 <= region_size <= 64 * 1024 * 1024:
        return {"ready": False, "reason": "invalid_mapping_size"}
    signature, version, revision = struct.unpack_from("<III", header)
    sensor_offset, sensor_size, sensor_count, entry_offset, entry_size, entry_count = struct.unpack_from("<IIIIII", header, 20)
    sensor_end = sensor_offset + sensor_size * sensor_count
    entry_end = entry_offset + entry_size * entry_count
    if (signature != 0x53695748 or sensor_offset < 48 or entry_offset < 48
            or sensor_size < 264 or entry_size < 316 or not 0 < sensor_count <= 4096
            or not 0 < entry_count <= 65536 or sensor_end > region_size or entry_end > region_size
            or (sensor_offset < entry_end and entry_offset < sensor_end)):
        return {"ready": False, "reason": "malformed_sm2_header", "signature": hex(signature),
                "sensor_count": sensor_count, "reading_count": entry_count}
    return {"ready": True, "mapping": r"Global\HWiNFO_SENS_SM2", "version": version,
            "revision": revision, "sensor_count": sensor_count, "reading_count": entry_count,
            "validation": "readable_header_and_bounded_nonoverlapping_sections",
            "sensor_sample_freshness": "verified_by_host_receiver_separately"}


def probe_hwinfo_shared_memory():
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.OpenFileMappingW.argtypes = [W.DWORD, W.BOOL, W.LPCWSTR]
    kernel.OpenFileMappingW.restype = W.HANDLE
    kernel.MapViewOfFile.argtypes = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, C.c_size_t]
    kernel.MapViewOfFile.restype = C.c_void_p
    kernel.UnmapViewOfFile.argtypes = [C.c_void_p]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    class MemoryBasicInformation(C.Structure):
        _fields_ = [("BaseAddress", C.c_void_p), ("AllocationBase", C.c_void_p),
                    ("AllocationProtect", W.DWORD), ("PartitionId", W.WORD),
                    ("RegionSize", C.c_size_t), ("State", W.DWORD),
                    ("Protect", W.DWORD), ("Type", W.DWORD)]
    kernel.VirtualQuery.argtypes = [C.c_void_p, C.POINTER(MemoryBasicInformation), C.c_size_t]
    kernel.VirtualQuery.restype = C.c_size_t
    handle = kernel.OpenFileMappingW(4, False, r"Global\HWiNFO_SENS_SM2")
    if not handle:
        return {"ready": False, "reason": "mapping_unavailable", "win32_error": C.get_last_error()}
    view = None
    try:
        view = kernel.MapViewOfFile(handle, 4, 0, 0, 0)
        if not view:
            return {"ready": False, "reason": "map_view_failed", "win32_error": C.get_last_error()}
        region = MemoryBasicInformation()
        if not kernel.VirtualQuery(view, C.byref(region), C.sizeof(region)):
            return {"ready": False, "reason": "virtual_query_failed", "win32_error": C.get_last_error()}
        if region.RegionSize < 48:
            return {"ready": False, "reason": "mapping_too_short"}
        return validate_sm2_header(C.string_at(view, 48), int(region.RegionSize))
    finally:
        if view:
            kernel.UnmapViewOfFile(view)
        kernel.CloseHandle(handle)


class HWiNFOReadyError(RuntimeError):
    def __init__(self, message, diagnostic):
        super().__init__(message)
        self.diagnostic = diagnostic


def start_hwinfo_sensors(config):
    executable = Path(config["hwinfo"])
    for path in (executable, UI_HELPER):
        if not path.is_file():
            raise FileNotFoundError(path)
    subprocess.Popen([str(executable)], cwd=executable.parent)
    ui = Desktop()
    window = ui.wait_window(r"^HWiNFO")
    ui.focus(window[0])
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-File", str(UI_HELPER),
         "-TargetProcessId", str(window[2]), "-NamePattern", r"(?i)^(Sensors.?only|仅传感器|传感器模式)$",
         "-Kind", "CheckBox", "-EnsureOn"], capture_output=True, encoding="utf-8",
        timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("Could not verify HWiNFO Sensors-only startup checkbox")
    if config.get("hwinfo_start_point"):
        ui.click(*config["hwinfo_start_point"])
    else:
        ui.control(window[2], r"(?i)^(Start|Run|开始|运行)$", "Button")


def ensure_hwinfo(config, process_query, *, probe=None, launch=None, clock=None, sleep=None):
    """An existing HWiNFO instance is only observed, never clicked/restarted."""
    probe = probe_hwinfo_shared_memory if probe is None else probe
    launch = start_hwinfo_sensors if launch is None else launch
    clock = time.monotonic if clock is None else clock
    sleep = time.sleep if sleep is None else sleep
    names = config.get("hwinfo_process_names", ["HWiNFO64.exe", "HWiNFO32.exe", "HWiNFO.exe"])
    def processes():
        return sorted({pid for name in names for pid in process_query(name.lower())})
    timeout = float(config.get("hwinfo_ready_timeout_seconds", 60))
    interval = float(config.get("hwinfo_poll_seconds", .5))
    if timeout < 0 or interval <= 0:
        raise ValueError("Invalid HWiNFO readiness timing")
    started = clock()
    pids, interface = processes(), probe()
    mode = "reused"
    if not pids:
        # A mapping without a recognized process is ambiguous. Do not launch a
        # potential second instance merely because its executable name differs.
        if interface.get("ready"):
            raise HWiNFOReadyError("SM2 exists without a recognized HWiNFO process; no instance started",
                                   {"status": "failed", "process_ids": [], "interface": interface})
        pids = processes()  # autostart may have arrived since the first query
        if not pids:
            launch(config)
            mode = "started_missing_instance"
    while True:
        pids, interface = processes(), probe()
        diagnostic = {"status": "ready" if pids and interface.get("ready") else "waiting",
                      "mode": mode, "process_ids": pids, "interface": interface,
                      "waited_seconds": clock() - started}
        if diagnostic["status"] == "ready":
            return diagnostic
        if clock() - started >= timeout:
            diagnostic["status"] = "failed"
            raise HWiNFOReadyError("HWiNFO process/SM2 interface did not become ready; existing instance was not restarted",
                                   diagnostic)
        sleep(interval)


def export_zen_cli(config, destination):
    """Exactly one CLI invocation. There is deliberately no ZenTimings GUI path."""
    options = config.get("zentimings_cli")
    if not isinstance(options, dict) or not options.get("executable"):
        raise ValueError("zentimings_cli.executable is required; GUI export is unsupported")
    executable = Path(options["executable"])
    if executable.name.lower() != "zentimings.export.exe":
        raise ValueError("Use the separately built ZenTimings.Export.exe, not the GUI application")
    if not executable.is_file():
        raise FileNotFoundError(executable)
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    log_path = destination.with_suffix(".cli.log")
    if log_path.exists():
        raise FileExistsError(log_path)
    started = time.monotonic()
    result = subprocess.run([str(executable), "--export-json", str(destination), "--exit"],
                            cwd=executable.parent, capture_output=True, encoding="utf-8", errors="replace",
                            timeout=float(options.get("timeout_seconds", 180)),
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    with log_path.open("x", encoding="utf-8") as stream:
        stream.write(f"exit_code={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}\n")
    if result.returncode:
        raise RuntimeError(f"ZenTimings CLI export failed with exit code {result.returncode}; see {log_path.name}")
    data = validate_zen(destination)
    if data.get("capture_mode") != "one_shot_cli":
        raise RuntimeError("ZenTimings export is not a live one-shot CLI snapshot")
    return {"mode": "one_shot_cli", "executable": str(executable), "executable_sha256": digest(executable),
            "exit_code": result.returncode, "duration_s": time.monotonic() - started, "log": str(log_path)}
