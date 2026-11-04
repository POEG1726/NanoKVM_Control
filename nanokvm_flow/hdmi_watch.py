"""Independent HDMI observer: synchronous SSH cannot delay video timestamps."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from . import boot_timing
from .client import NativeMCP
from .notify import Notifier, _run_lock
from .state import Run, utc
from .vision import read_text


def _write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def _alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]; kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0); return True
    except OSError:
        return False


def start_background(config, run):
    """Spawn only from a named existing config; never serialize credentials."""
    if (run.path / 'STOP').exists():
        return {'status': 'stopped'}
    config_path = config.get('_config_path')
    if not config_path:
        return {'status': 'not_configured'}
    status_file = run.path / 'hdmi-watch.json'
    if status_file.exists():
        previous = json.loads(status_file.read_text(encoding='utf-8'))
        if previous.get('status') == 'watching' and _alive(previous.get('pid')):
            return {'status': 'already_running', 'pid': previous['pid']}
    command = [sys.executable, '-m', 'nanokvm_flow.hdmi_watch', '--config',
               str(Path(config_path).resolve()), '--run', run.name]
    with (run.path / 'hdmi-watch.log').open('ab') as log:
        process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1],
                                   stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    run.event('hdmi_watcher_requested', pid=process.pid)
    return {'status': 'requested', 'pid': process.pid}


async def watch(config, run, *, client_factory=NativeMCP, ocr=read_text):
    notify = Notifier(config)
    path = run.path / 'hdmi-watch.json'
    previous = None
    last_error = None
    while True:
        if (run.path / 'STOP').exists():
            _write(path, {'status': 'stopped', 'pid': os.getpid(), 'utc': utc()}); return
        timing_file = run.path / 'boot-timing.json'
        desktop_marker = run.path / 'desktop-ready.json'
        desktop_seen = desktop_marker.exists()
        if timing_file.exists():
            desktop_seen = desktop_seen or json.loads(timing_file.read_text(encoding='utf-8'))['endpoints']['desktop_ready'] is not None
        if desktop_seen:
            attempt = run.path / 'dsview-stop-attempt.json'
            if not attempt.exists():
                try:
                    start_receipt=run.path/'dsview-start.json'
                    if not start_receipt.exists():
                        _write(attempt, {'status':'skipped_not_started_by_run','utc':utc()})
                    else:
                        from .dsview import stop_capture, command
                        receipt=json.loads(start_receipt.read_text(encoding='utf-8'))
                        expected=receipt['state'].get('output_directory') or receipt.get('output_directory')
                        current=await asyncio.to_thread(command,config,'status')
                        actual=current.get('output_directory')
                        if not expected or not actual or not Path(actual).resolve().is_relative_to(Path(expected).resolve()):
                            raise RuntimeError('DSView recording changed; leaving unrelated capture untouched')
                        _write(attempt, {'status':'requested','utc':utc()})
                        response = await asyncio.to_thread(stop_capture, config, run)
                        _write(attempt, {'status': 'finished', 'utc': utc(), 'receipt': response})
                except Exception as error:
                    from .workflow import capture_warning
                    capture_warning(config, run, 'stop', error)
                    _write(attempt, {'status': 'failed', 'utc': utc(), 'error_type': type(error).__name__})
            await asyncio.to_thread(notify.drain, run)
            _write(path, {'status': 'desktop_ready', 'pid': os.getpid(), 'utc': utc()}); return
        if not timing_file.exists() and boot_timing.adopt_legacy(run) is None:
            await asyncio.sleep(.25); continue  # F10 send/ack may still be in progress.
        timing = json.loads(timing_file.read_text(encoding='utf-8'))
        try:
            started_utc, started_ns = utc(), time.monotonic_ns()
            async with client_factory(config) as client:
                picture, metadata = await client.screenshot()
            ended_ns, ended_utc = time.monotonic_ns(), utc()
            available = bool(picture)
            timing = boot_timing.observe(run, available, started_utc, ended_utc,
                                         started_ns, ended_ns, metadata)
            observation = {'status': 'watching', 'pid': os.getpid(), 'utc': ended_utc,
                           'available': available, 'classification': 'unknown',
                           'boot_duration_s': timing['boot_duration_s']}
            if picture:
                image_path = run.path / 'boot-observation.jpg'
                image_path.write_bytes(picture)
                if previous is False:
                    permanent = run.path / ('hdmi-restored-' + str(ended_ns) + '.jpg')
                    permanent.write_bytes(picture)
                    notify.notify(run, 'video_returned', 'Valid HDMI output restored after signal absence; boot timing records whether this is the first or a later restoration. This is not desktop readiness.', permanent)
                observed = await asyncio.to_thread(ocr, image_path)
                observation['classification'] = observed.get('classification', 'unknown')
                if observation['classification'] == 'failure':
                    permanent = run.path / ('boot-failure-' + str(ended_ns) + '.jpg')
                    permanent.write_bytes(picture)
                    observation.update(status='boot_failure_detected', evidence_path=str(permanent))
                    _write(path, observation); return
            _write(path, observation)
            previous, last_error = available, None
            await asyncio.to_thread(notify.drain, run)
        except Exception as error:
            key = type(error).__name__
            _write(path, {'status': 'monitor_error', 'pid': os.getpid(), 'utc': utc(), 'error_type': key})
            if key != last_error:
                run.event('video_monitor_error', error_type=key)
                await asyncio.to_thread(notify.notify, run, 'video_monitor_error',
                                        'Independent HDMI monitoring is unavailable; SSH/test control continues. Inspect the monitor.', [])
            last_error = key
        await asyncio.sleep(config.get('hdmi_poll_seconds', 2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True); parser.add_argument('--run', required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    run = Run(config['runs_root'], args.run)
    with _run_lock(run.path / 'hdmi-watch.lock'):
        asyncio.run(watch(config, run))


if __name__ == '__main__':
    main()
