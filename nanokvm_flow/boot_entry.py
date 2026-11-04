"""One reboot, recognized BIOS anchor, optional calibrated apply; pause at F10."""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import time

from .client import NativeMCP
from .bios import build_actions
from .dsview import ensure_capture_started
from .host import Target
from .notify import Notifier, _run_lock
from .state import Run, utc, sha
from .workflow import capture_warning, wait_video, execute


class EntryStopped(asyncio.CancelledError):
    pass


def _stop(run):
    if (run.path / 'STOP').exists():
        raise EntryStopped()


def _event(run, event, **details):
    run.event(event, **details)
    with (run.path / 'boot_entry.log').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'utc': utc(), 'event': event, **details}) + '\n')


def _write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    temporary.replace(path)


class _CheckedClient:
    def __init__(self, client, run, poll_s):
        self.client, self.run, self.poll_s = client, run, poll_s

    async def keys(self, keys):
        _stop(self.run)
        return await self.client.keys(keys)

    async def text(self, text):
        _stop(self.run)
        return await self.client.text(text)

    async def screenshot(self):
        previous_error = None
        while True:
            _stop(self.run)
            try:
                result = await self.client.screenshot()
                _stop(self.run)
                return result
            except Exception as error:
                kind = type(error).__name__
                if kind != previous_error:
                    _event(self.run, 'boot_entry_video_error', error_type=kind)
                previous_error = kind
                await asyncio.sleep(self.poll_s)


async def enter_bios(config, run, *, target=None, client_factory=NativeMCP, poll_s=.5,
                     firmware_ui=False, auto_apply=False):
    auto_apply = auto_apply or config.get('bios_entry_auto_apply', False)
    if run.load()['stage'] not in ('created', 'awaiting_bios_anchor'):
        raise RuntimeError('Boot entry requires a new run or an unapproved BIOS anchor')
    actions = None
    if auto_apply:
        profile = run.load()['profile']
        if (profile.get('route', {}).get('selector', {}).get('mode') != 'numeric'
                or profile.get('route', {}).get('start_anchor', {}).get('id') != 'from_tweaker_voltage_anchor'
                or profile.get('profile_storage', {}).get('slot') != 8
                or profile.get('profile_storage', {}).get('name') != 'agent-tune'
                or profile.get('save_before_f10') is not True):
            raise RuntimeError('Auto-apply requires the calibrated numeric Tweaker route and Profile8 agent-tune')
        actions = build_actions(profile, run.load()['slot'])
    receipt_path = run.path / 'restart-request.json'
    if receipt_path.exists():
        return {'status': 'restart_already_attempted', 'receipt': json.loads(receipt_path.read_text())}
    target = target if target is not None else Target(config)
    try:
        _stop(run)
        stress = json.loads(await asyncio.to_thread(target.ps,
            "$p=@(Get-CimInstance Win32_Process | Where-Object {$_.Name -in @('TM5.exe','y-cruncher.exe','24-ZN5 ~ Komari.exe')}); [pscustomobject]@{stress=@($p | Select-Object ProcessId,Name)} | ConvertTo-Json -Compress"))
        if stress.get('stress'):
            raise RuntimeError('A target stress process is running; reboot was not requested')
        _stop(run)
        async with client_factory(config) as native:
            client = _CheckedClient(native, run, poll_s)
            try:
                await asyncio.to_thread(ensure_capture_started, config, run)
            except Exception as error:
                capture_warning(config, run, 'start', error)
            _stop(run)
            restart_command = 'shutdown /r /fw /t 0' if firmware_ui else 'shutdown /r /t 0'
            receipt = {'status': 'dispatching', 'requested_utc': utc(),
                       'requested_monotonic_ns': time.monotonic_ns(),
                       'command': restart_command, 'firmware_ui': firmware_ui,
                       'meaning': 'Not yet an acknowledgement'}
            # An ambiguous/crashed request is never automatically replayed.
            with receipt_path.open('x', encoding='utf-8') as stream:
                json.dump(receipt, stream)
            try:
                arguments = '/r /fw /t 0' if firmware_ui else '/r /t 0'
                output = await asyncio.to_thread(target.ps,
                    "& shutdown.exe " + arguments + "; $code=$LASTEXITCODE; [pscustomobject]@{requested=($code -eq 0);exit_code=$code} | ConvertTo-Json -Compress")
                response = json.loads(output)
                receipt.update(status='acknowledged' if response.get('requested') is True else 'rejected_not_initiated',
                               response=response, acknowledged_utc=utc())
            except Exception as error:
                receipt.update(status='unconfirmed_transport', error_type=type(error).__name__,
                               meaning='SSH failed after dispatch began; initiation is unknown; do not resend')
            _write(receipt_path, receipt)
            _event(run, 'boot_entry_restart_request', receipt=receipt)
            if receipt['status'] == 'rejected_not_initiated':
                raise RuntimeError('Windows rejected restart; no Delete keys were sent')
            while True:  # No total POST timeout; no keys while Windows still has HDMI.
                picture, _ = await client.screenshot()
                if not picture:
                    _event(run, 'boot_entry_hdmi_absence_observed')
                    break
                await asyncio.sleep(poll_s)
            previous_probe_error = None

            async def windows_probe():
                nonlocal previous_probe_error
                try:
                    output = await asyncio.to_thread(target.ps,
                        "$p=@(Get-Process explorer -ErrorAction SilentlyContinue | Where-Object {$_.SessionId -gt 0}); if($p.Count){[pscustomobject]@{source='interactive_explorer_process_via_ssh';session_id=$p[0].SessionId;explorer_pid=$p[0].Id;boot_id=(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')} | ConvertTo-Json -Compress}")
                    data = json.loads(output) if output else {}
                    if (not data.get('explorer_pid') or data.get('session_id', 0) <= 0
                            or not data.get('boot_id')):
                        return None
                    boot = datetime.fromisoformat(data['boot_id'].replace('Z', '+00:00'))
                    requested = datetime.fromisoformat(receipt['requested_utc'].replace('Z', '+00:00'))
                    return data if boot > requested else None
                except Exception as error:
                    kind = type(error).__name__
                    if kind != previous_probe_error:
                        _event(run, 'boot_entry_windows_probe_unavailable', error_type=kind)
                    previous_probe_error = kind
                    return None

            username = config.get('windows_username') or config.get('ssh_target', '').split('@')[0]
            entry = await wait_video(client, run, poll_s=poll_s,
                                     send_delete=not firmware_ui, classify_entry=True,
                                     windows_probe=windows_probe, windows_username=username or None)
            status = entry['status']
            image = entry.get('image')
            if status == 'stopped':
                _event(run, 'boot_entry_stopped')
                return entry
            if status == 'awaiting_bios_anchor':
                if not auto_apply:
                    _event(run, 'bios_entry_observed', image=image, notification='suppressed_normal_entry')
                    return entry
                if entry.get('tweaker_root', {}).get('matched') is not True:
                    run.update(stage='entry_unclassified')
                    status = 'entry_unclassified'
                    entry.update(status=status, reason='recognized_bios_page_is_not_calibrated_tweaker_root')
                else:
                    anchor = {**image, 'ocr_evidence': entry['ocr_evidence'],
                              'ocr_sha256': sha(run.path / entry['ocr_evidence']),
                              'root_match': entry['tweaker_root']}
                    run.update(stage='bios_anchor_confirmed', anchor_image=anchor)
                    _event(run, 'calibrated_tweaker_root_confirmed', anchor=anchor)
                    request = await execute(client, run, actions)
                    if run.load()['stage'] != 'awaiting_f10_review':
                        raise RuntimeError('Auto-apply did not stop at the F10 review boundary')
                    _event(run, 'auto_apply_awaiting_f10_review', request_sha256=request['request_sha256'])
                    # Only the reviewed confirmation path may send the final Enter.
                    result = Notifier(config).notify(run, 'f10_review',
                        'Review the complete F10 change list and Profile8 agent-tune. Auto-apply paused; no save confirmation sent.',
                        run.load()['evidence'][-3:])
                    return {'status': 'awaiting_f10_review', 'request': request, 'notification': result}
            event_name, summary = {
                'awaiting_bios_anchor': ('bios_anchor_ready',
                    'BIOS text recognized after reboot. Inspect the actual BIOS/Tweaker anchor before apply; no values or F10 confirmation sent.'),
                'boot_failure_detected': ('boot_failure',
                    'Explicit Boot Failure screen recognized; entry paused for agent inspection. No recovery or automatic reboot sent.'),
                'missed_bios_entry': ('missed_bios_entry',
                    'Windows reached after the restart; BIOS entry missed. Entry paused; no automatic restart, BIOS change or test launch.'),
                'entry_unclassified': ('bios_entry_unclassified',
                    'Visible screen could not be safely classified. Entry paused for inspection; do not claim BIOS entry or automatically restart.')
            }[status]
            _event(run, event_name, image=image, classification=entry.get('classification'),
                   windows_evidence=entry.get('windows_evidence'))
            notifier = Notifier(config)
            result = notifier.notify(run, event_name, summary, image or [])
            while result['status'] in ('pending', 'deferred', 'busy'):
                _stop(run)
                await asyncio.sleep(30)
                result = notifier.notify(run, event_name, summary, image or [])
            return {**entry, 'notification': result}
    except EntryStopped:
        _event(run, 'boot_entry_stopped')
        return {'status': 'stopped', 'meaning': 'Only local entry script stopped; target untouched'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True); parser.add_argument('--run', required=True)
    parser.add_argument('--firmware-ui', action='store_true',
                        help='One-shot Windows UEFI firmware UI request; classify output without Delete or restart retry')
    parser.add_argument('--auto-apply', action='store_true',
                        help='After OCR confirms the calibrated Tweaker root, apply numeric route and pause for F10 agent review')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    run = Run(config['runs_root'], args.run)
    with _run_lock(run.path / 'boot_entry.lock'):
        try:
            result = asyncio.run(enter_bios(config, run, firmware_ui=args.firmware_ui, auto_apply=args.auto_apply))
        except Exception as error:
            _event(run, 'boot_entry_failed', error_type=type(error).__name__)
            Notifier(config).notify(run, 'boot_entry_failed',
                'BIOS entry/apply stopped for inspection. No automatic restart retry or F10 save confirmation; pending edits may exist. Error: ' + type(error).__name__, [])
            raise SystemExit(1) from None
    print(json.dumps(result))


if __name__ == '__main__':
    main()
