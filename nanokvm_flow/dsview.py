"""DSView native capture-control CLI adapter. No GUI automation or key toggles."""
import json
import os
from pathlib import Path
import subprocess
import time


class DSViewError(RuntimeError):
    pass


def options(config):
    if isinstance(config, (str, Path)):
        return json.loads(Path(config).read_text(encoding='utf-8-sig'))
    if config.get('dsview_config'):
        return options(config['dsview_config'])
    return config


def command(config, operation, directory=None):
    config = options(config)
    args = [config['executable'], '--capture-' + operation]
    if operation == 'start':
        args += [str(directory), '--capture-format', config.get('format', 'csv')]
    timeout = int(config.get('command_timeout_ms', 120000))
    args += ['--capture-timeout-ms', str(timeout)]
    if config.get('control_name'):
        args += ['--capture-control-name', config['control_name']]
    environment = os.environ.copy()
    # The historical GUI launcher forwards arguments/exit status but does not
    # inherit redirected stdout. Invoke the core client and give it its matching
    # portable DLL directory; do not start another GUI to obtain a response.
    binary_directory = Path(config['executable']).resolve().parent
    runtime_bin = binary_directory / 'runtime' / 'bin'
    if runtime_bin.is_dir():
        environment['PATH'] = str(runtime_bin) + os.pathsep + environment.get('PATH', '')
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=timeout / 1000 + 5, shell=False,
                                env=environment,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        response = json.loads(result.stdout.lstrip('\ufeff').strip())
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        raise DSViewError('DSView native CLI unavailable or invalid: ' + type(error).__name__) from error
    if result.returncode or response.get('ok') is not True:
        raise DSViewError(response.get('error') or 'DSView capture command failed')
    if response.get('schema_version') != 1:
        raise DSViewError('DSView capture-control protocol version mismatch')
    return response


def file_bytes(directory):
    path = Path(directory)
    if not path.is_dir():
        return 0
    # Metadata only; never read, rename or repair a recording file.
    return sum(p.stat().st_size for p in path.iterdir() if p.is_file())


def _active(state, config):
    return (state.get('working') is True and state.get('device_collecting') is True
            and state.get('recorder_running') is True and state.get('recorder_failed') is False
            and state.get('loop_mode') is True and state.get('stream_mode') is True
            and state.get('sample_rate_hz') == 50000000 and state.get('format') == config.get('format', 'csv')
            and bool(state.get('output_directory')))


def _write(run, name, response):
    folder = Path(run.path)
    path = folder / name
    # Keep previous receipts if an explicit retry is required.
    if path.exists():
        previous = folder / (path.stem + '_' + str(time.time_ns()) + path.suffix)
        path.rename(previous)
    path.write_text(json.dumps(response, indent=2), encoding='utf-8')


def ensure_capture_started(config, run, *, call=command, sleep=time.sleep, clock=time.monotonic, disk_bytes=file_bytes):
    """Request capture and return its state; never wait for bus data or file growth.

    The legacy observation keyword arguments remain accepted for callers, but
    are deliberately unused. The caller owns sequencing and error reporting.
    """
    config = options(config)
    if config.get('enabled') is not True:
        raise DSViewError('DSView capture is not enabled')
    state = call(config, 'status')
    reused = bool(state.get('working') or state.get('recorder_running') or state.get('device_collecting'))
    if reused:
        if not _active(state, config):
            raise DSViewError('Existing capture is incompatible or incomplete; left untouched')
    else:
        output = Path(config['output_root']) / run.name
        if output.exists():
            raise DSViewError('Recording root already exists; refusing overwrite: ' + str(output))
        state = call(config, 'start', output)
        if state.get('start_requested') is not True:
            raise DSViewError('DSView did not accept the capture start request')
    receipt = {'status': 'capture_reused' if reused else 'capture_start_requested',
               'output_directory': state.get('output_directory') or str(output),
               'reused': reused, 'state': state}
    _write(run, 'dsview-start.json', receipt)
    return receipt


def stop_capture(config, run, *, call=command):
    config = options(config)
    response = call(config, 'stop')
    if (response.get('flushed') is not True or response.get('working') is not False
            or response.get('device_collecting') is not False or response.get('recorder_running') is not False
            or response.get('recorder_failed') is not False or response.get('queued_bytes') != 0):
        raise DSViewError('DSView stop/flush was not confirmed')
    folder = response.get('output_directory')
    if folder and any(p.name.endswith('.part') for p in Path(folder).iterdir() if p.is_file()):
        raise DSViewError('DSView stopped with partial recording files; not claiming a complete capture')
    receipt = {'status': 'stopped_and_flushed', 'state': response}
    _write(run, 'dsview-stop.json', receipt)
    return receipt
