"""File-only telemetry upgrade. Never starts/stops collectors or test workloads."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path, PureWindowsPath
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nanokvm_flow.host import Target, TargetUnavailable, quote


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_bundle(bundle):
    manifest = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
    files = manifest['files']
    if len(files) != 2 or {f['role'] for f in files} != {'executable', 'filter'}:
        raise ValueError('Expected exactly one executable and one filter')
    for item in files:
        name = item['name']
        relative = PureWindowsPath(item['remote_relative'])
        if Path(name).name != name or relative.drive or relative.root or '..' in relative.parts:
            raise ValueError('Invalid bundle path')
        source = bundle / name
        if digest(source) != item['sha256'] or source.stat().st_size != item['size']:
            raise ValueError('Bundle hash/size mismatch')
    return manifest


def install(target, bundle, manifest, remote_root):
    root = PureWindowsPath(remote_root)
    cfg = root / 'collector.json'
    target.ps(f"if(!(Test-Path -LiteralPath {quote(cfg)})){{throw 'Collector configuration missing'}}")
    # Upload unique versioned files, then atomically select them for future starts.
    for item in manifest['files']:
        destination = root / item['remote_relative']
        incoming = str(destination) + '.incoming'
        target.ps(f"New-Item -ItemType Directory -Force -Path {quote(destination.parent)} | Out-Null")
        target.copy(bundle / item['name'], incoming, upload=True)
        target.ps(f"if((Get-FileHash -LiteralPath {quote(incoming)} -Algorithm SHA256).Hash -ne {quote(item['sha256'])}){{throw 'Probe transfer hash mismatch'}}; Move-Item -LiteralPath {quote(incoming)} -Destination {quote(destination)} -Force")
    roles = {item['role']: item['remote_relative'] for item in manifest['files']}
    # Existing receiver command stays entirely on the target; never return/log it.
    result = target.ps(f"""
$p={quote(cfg)}
$original=[IO.File]::ReadAllText($p)
$c=$original | ConvertFrom-Json
$c | Add-Member -NotePropertyName executable -NotePropertyValue {quote(roles['executable'])} -Force
$c | Add-Member -NotePropertyName filter -NotePropertyValue {quote(roles['filter'])} -Force
$next=$c | ConvertTo-Json -Depth 20
if([IO.File]::ReadAllText($p) -cne $original){{throw 'Configuration changed during upgrade'}}
$tmp=$p+'.probe-upgrade.tmp'
[IO.File]::WriteAllText($tmp,$next,[Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $tmp -Destination $p -Force
$active=@(Get-Process -Name 'hwtrace*' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
[pscustomobject]@{{status='installed_for_next_collector_start';running_collector_pids=$active;executable=$c.executable;filter=$c.filter}} | ConvertTo-Json -Compress
""")
    return json.loads(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config/host.json')
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--remote-root', default=r'D:\MyProj\HWTrace')
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    manifest = validate_bundle(bundle)
    receipt = bundle / 'deployment.json'
    target = Target(json.loads(Path(args.config).read_text(encoding='utf-8-sig')))
    state = {'status': 'waiting_for_target', 'started_utc': dt.datetime.now(dt.timezone.utc).isoformat()}
    receipt.write_text(json.dumps(state, indent=2), encoding='utf-8')
    while True:
        if (bundle / 'STOP').exists():
            state['status'] = 'cancelled_without_process_control'
            break
        try:
            state.update(install(target, bundle, manifest, args.remote_root))
            break
        except TargetUnavailable:
            if not args.watch:
                break
            time.sleep(20)
        except Exception as error:
            # Subprocess diagnostics may contain transport configuration; log only class.
            state.update(status='deployment_error', error_class=type(error).__name__)
            break
    state['updated_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
    receipt.write_text(json.dumps(state, indent=2), encoding='utf-8')
    print(json.dumps(state))


if __name__ == '__main__':
    main()
