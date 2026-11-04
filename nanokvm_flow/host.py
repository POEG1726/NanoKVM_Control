"""SSH artifact transfer and target orchestration using the existing SSH identity."""
import base64
import datetime as dt
import json
from pathlib import Path, PureWindowsPath
import sqlite3
import math
import subprocess
import sys
import tempfile
import time
import uuid
from .state import sha


class TargetUnavailable(ConnectionError):
    pass


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


class Target:
    def __init__(self, config):
        self.config = config
        self.root = PureWindowsPath(config['target_root'])

    def ps(self, code):
        script = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); " + code
        encoded = base64.b64encode(script.encode('utf-16-le')).decode()
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                                 self.config['ssh_target'], 'pwsh', '-NoProfile',
                                 '-OutputFormat', 'Text', '-EncodedCommand', encoded],
                                capture_output=True, text=True, encoding='utf-8', timeout=45)
        if result.returncode:
            if result.returncode == 255:
                raise TargetUnavailable('Target SSH is unavailable')
            raise RuntimeError('Target SSH command failed')
        return result.stdout.lstrip('\ufeff').strip()

    def write_json(self, path, obj):
        # Transfer data through SCP rather than nesting it in EncodedCommand:
        # even a small receipt can exceed the Windows SSH command-line limit.
        path = PureWindowsPath(path)
        pending = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
        with tempfile.TemporaryDirectory(prefix='nanokvm-json-') as directory:
            local = Path(directory) / 'payload.json'
            local.write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')
            expected = sha(local)
            size = local.stat().st_size
            self.copy(local, pending, upload=True)
            # Same-directory atomic replacement only after the uploaded bytes
            # are verified. Failed uploads/checks never replace the final file.
            return self.ps(
                f"$p={quote(pending)}; if((Get-Item -LiteralPath $p).Length -ne {size} "
                f"-or (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash -ne {quote(expected)})"
                "{throw 'JSON transfer hash/size mismatch'}; "
                f"[IO.File]::Move($p,{quote(path)},$true)")

    def copy(self, source, destination, upload=False):
        remote = self.config['ssh_target'] + ':' + PureWindowsPath(destination if upload else source).as_posix()
        arguments = [str(source), remote] if upload else [remote, str(destination)]
        subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', *arguments],
                       check=True, capture_output=True, timeout=120)

    def arm(self, run):
        return self.write_json(self.root / 'pending.json', {'run_id': run.name})

    def desktop_ready(self, run):
        path=self.root/'runs'/run.name/'desktop-ready.json'
        output=self.ps(f"if(Test-Path -LiteralPath {quote(path)}){{Get-Content -LiteralPath {quote(path)} -Raw}}")
        if not output:
            return None
        marker=json.loads(output)
        if (marker.get('run_id')!=run.name or marker.get('desktop_ready') is not True
                or not marker.get('explorer_pid') or marker.get('session_id',0)<=0):
            raise RuntimeError('Invalid target desktop-ready marker')
        return marker

    def deploy_zentimings_cli(self):
        """Copy the headless build to its own versioned folder; leave GUI intact."""
        source=Path(self.config['zentimings_cli_bundle'])
        manifest_path=source/'build-manifest.json'
        manifest=json.loads(manifest_path.read_text(encoding='utf-8-sig'))
        files=manifest['files']
        expected={'ZenTimings.Export.exe','ZenStates-Core.dll','inpoutx64.dll','ZenTimings.Export.exe.config'}
        if len(files)!=4 or {item['name'] for item in files}!=expected:
            raise RuntimeError('Incomplete headless ZenTimings build')
        for item in files:
            path=source/item['name']
            if path.stat().st_size!=item['size'] or sha(path)!=item['sha256']:
                raise RuntimeError('Headless ZenTimings build hash mismatch')
        remote=self.root/'ZenTimingsCLI'/sha(manifest_path)[:16]
        self.ps(f"New-Item -ItemType Directory -Path {quote(remote)} -Force | Out-Null")
        for item in files:
            self.copy(source/item['name'],remote/item['name'],upload=True)
        self.copy(manifest_path,remote/'build-manifest.json',upload=True)
        checks=';'.join(
            f"if((Get-FileHash -LiteralPath {quote(remote/item['name'])} -Algorithm SHA256).Hash -ne {quote(item['sha256'])}){{throw 'CLI transfer hash mismatch'}}"
            for item in files)
        self.ps(checks)
        return str(remote/'ZenTimings.Export.exe')

    def deploy(self, source_root, run, start_capture=False):
        root=Path(source_root)
        self.ps(f"New-Item -ItemType Directory -Path {quote(self.root)} -Force | Out-Null")
        for path in sorted(root.iterdir()):
            if path.suffix in ('.py','.ps1'):
                self.copy(path,self.root/path.name,upload=True)
        config_path=self.root/'config.json'
        exists=self.ps(f"Test-Path -LiteralPath {quote(config_path)}").lower()=='true'
        if exists:
            config=json.loads(self.ps(f"Get-Content -LiteralPath {quote(config_path)} -Raw"))
        else:
            config=json.loads((root/'config.example.json').read_text(encoding='utf-8-sig'))
        config.pop('zentimings',None)
        config.pop('zen_export_menu_point',None)
        config['zentimings_cli']={**config.get('zentimings_cli',{}),'executable':self.deploy_zentimings_cli()}
        config['tests']['vt3'].update(repeats=10,expected_count=10)
        self.write_json(config_path,config)
        # The installer never launches; bootstrapping explicitly starts just this
        # capture task once after installing the future-login trigger.
        task=self.ps("$t=Get-ScheduledTask | Where-Object {$_.TaskName -eq 'OC-ScriptedBootCapture'}; if($t){$t.Actions | Select-Object Execute,Arguments | ConvertTo-Json -Compress}")
        if task:
            if str(self.root/'pipeline.py').lower() not in json.loads(task)['Arguments'].lower():
                raise RuntimeError('Existing capture task points to another installation')
        else:
            self.ps(f"& {quote(self.root/'Install-AtLogon.ps1')} -ConfigPath {quote(config_path)}")
        self.arm(run)
        if start_capture:
            self.ps("Start-ScheduledTask -TaskName 'OC-ScriptedBootCapture'")
        return {'deployed':True,'run_id':run.name,'capture_requested':start_capture}

    def command(self, verb, run, kind=None):
        args = f"& {quote(self.config['target_python'])} {quote(self.root / 'pipeline.py')} {verb} --config {quote(self.root / 'config.json')} --run-id {quote(run.name)}"
        if kind:
            args += ' --kind ' + quote(kind)
        return self.ps(args)

    def fetch_ready(self, run):
        folder = self.root / 'runs' / run.name
        output = self.ps(f"$p={quote(folder / 'manifest.json')}; $c={quote(folder / 'complete.json')}; $e={quote(folder / 'failed.json')}; if(Test-Path -LiteralPath $e){{throw 'Target capture failed; inspect failed.json'}}; if((Test-Path -LiteralPath $p) -and (Test-Path -LiteralPath $c)){{Get-Content -LiteralPath $p -Raw}}")
        if not output:
            return None
        manifest = json.loads(output)
        if manifest.get('run_id') != run.name:
            raise RuntimeError('Wrong run manifest')
        if PureWindowsPath(manifest['folder']) != folder:
            raise RuntimeError('Wrong source folder')
        local = run.path / 'target'
        local.mkdir(exist_ok=True)
        manifest_file = local / 'manifest.json'
        if not manifest_file.exists():
            self.copy(folder / 'manifest.json', manifest_file)
        recorded = json.loads(manifest_file.read_text(encoding='utf-8-sig'))
        if recorded != manifest:
            # A collector restart intentionally rotates the manifest binding.
            # Preserve the previous version rather than silently overwriting it.
            previous = local / ('manifest_' + sha(manifest_file) + '.json')
            if previous.exists():
                raise RuntimeError('Repeated manifest rotation requires inspection')
            manifest_file.rename(previous)
            self.copy(folder / 'manifest.json', manifest_file)
            if json.loads(manifest_file.read_text(encoding='utf-8-sig')) != manifest:
                raise RuntimeError('Target manifest changed while downloading')
        if len(manifest['files']) != 2 or {Path(f['name']).suffix for f in manifest['files']} != {'.json', '.bin'}:
            raise RuntimeError('Missing dump or ZenTimings JSON')
        for f in manifest['files']:
            name = f['name']
            if Path(name).name != name or '/' in name or '\\' in name or name in ('.', '..'):
                raise RuntimeError('Unsafe artifact path')
            if name.endswith('.bin') and f['size'] != 33554432:
                raise RuntimeError('Incorrect dump size')
            final = local / name
            if not final.exists():
                temporary = local / (name + '.part')
                self.copy(folder / name, temporary)
                if sha(temporary) != f['sha256'] or temporary.stat().st_size != f['size']:
                    raise RuntimeError('Transferred artifact failed verification')
                temporary.rename(final)
            if sha(final) != f['sha256'] or final.stat().st_size != f['size']:
                raise RuntimeError('Existing artifact mismatch')
        return manifest, manifest_file

    def receipt(self, run, manifest, manifest_file, telemetry, readback=None):
        receipt = {'run_id': run.name, 'manifest_sha256': sha(manifest_file),
                   'files': manifest['files'], 'telemetry_verified': telemetry,
                   'readback_verified': True}
        if readback is not None:
            receipt['readback'] = readback
        local = run.path / 'received.json'
        local.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        self.write_json(self.root / 'runs' / run.name / 'received.json', receipt)


def verify_saved_vddp(manifest, local_folder, expected_mv, verification):
    """Read only the hash-bound local dump; never infer a 5mV slot from SMU."""
    reader = Path(verification.get('saved_request_reader', ''))
    if not reader.is_absolute() or not reader.is_file():
        raise RuntimeError('Exact VDDP saved-request reader must be an existing absolute path')
    entries = [f for f in manifest['files'] if f['name'].endswith('.bin')]
    if len(entries) != 1:
        raise RuntimeError('Exact VDDP verification requires one dump')
    entry = entries[0]
    name = entry['name']
    if Path(name).name != name or '/' in name or '\\' in name:
        raise RuntimeError('Unsafe dump artifact path')
    dump = (Path(local_folder) / name).resolve()
    if dump.stat().st_size != entry['size'] or sha(dump) != entry['sha256']:
        raise RuntimeError('Exact VDDP dump does not match the received manifest')
    try:
        completed = subprocess.run(
            [sys.executable, str(reader), '--dump', str(dump), '--expected-mv', str(expected_mv)],
            capture_output=True, text=True, encoding='utf-8', timeout=30)
    except subprocess.TimeoutExpired as error:
        # A parser failure is terminal; worker POST timeout handling must not retry it.
        raise RuntimeError('Exact VDDP saved-request reader timed out') from error
    try:
        result = json.loads(completed.stdout)
    except (ValueError, TypeError) as error:
        raise RuntimeError('Exact VDDP saved-request reader returned invalid JSON') from error
    if (not isinstance(result, dict) or completed.returncode != 0
            or result.get('ok') is not True or result.get('exact_match') is not True
            or result.get('manual_match') is not True or result.get('expected_mv') != expected_mv):
        raise RuntimeError(f'Exact VDDP saved requests do not verify {expected_mv}mV')
    binding = result.get('dump', {})
    if (binding.get('sha256') != entry['sha256'] or binding.get('size') != entry['size']
            or binding.get('unchanged_after_verification') is not True
            or sha(dump) != entry['sha256']):
        raise RuntimeError('Exact VDDP result is not bound to the unchanged received dump')
    return {'reader': str(reader), 'reader_sha256': sha(reader), 'result': result,
            'scope': 'Exact active saved BIOS requests and Manual selector; not a physical rail measurement'}


def verify_vddp(manifest, local_folder, expected_mv, tolerance_v=.020, *, profile=None):
    verification = (profile or {}).get('readback_verification', {})
    saved = None
    if verification.get('require_exact_saved_requests'):
        saved = verify_saved_vddp(manifest, local_folder, expected_mv, verification)
    entry = next(f for f in manifest['files'] if f['name'].endswith('.json'))
    data = json.loads((Path(local_folder) / entry['name']).read_text(encoding='utf-8-sig'))
    actual = data['readings']['power_table']['cldo_vddp_v']
    if isinstance(actual, bool) or not isinstance(actual, (int, float)) or not math.isfinite(actual):
        raise RuntimeError('Invalid VDDP readback')
    if abs(actual - expected_mv / 1000) > tolerance_v:
        raise RuntimeError(f'VDDP reported {actual:.4f}V does not match {expected_mv}mV')
    result = {'reported_v': actual, 'expected_mv': expected_mv, 'tolerance_v': tolerance_v,
            'scope': 'ZenTimings power-table report; not a physical rail measurement or proof of every BIOS request'}
    if saved is not None:
        result['saved_request_verification'] = saved
        result['reported_voltage_scope'] = 'Independent SMU sanity check; tolerance does not distinguish adjacent 5mV requests'
    return result


def telemetry_current(database, pid, created_utc, max_age_s=45):
    lower = f'{pid:08x}-'; upper = f'{pid+1:08x}-'
    conn = sqlite3.connect(Path(database).as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        conn.execute('pragma query_only=on')
        row = conn.execute('select session,seq,timestamp from records where session>=? and session<? and kind=? order by timestamp desc limit 1',
                           (lower, upper, 'sample')).fetchone()
        if not row or row[2] < created_utc:
            return None
        timestamp = dt.datetime.fromisoformat(row[2].replace('Z', '+00:00'))
        if (dt.datetime.now(dt.timezone.utc) - timestamp).total_seconds() > max_age_s:
            return None
        return {'session': row[0], 'seq': row[1], 'timestamp': row[2]}
    finally:
        conn.close()
