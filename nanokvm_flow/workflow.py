"""Scripted HID, quiet POST wait, and a non-bypassable F10 pause."""
import asyncio
import contextlib
import hashlib
import json
import re
import time
from .state import utc
from .vision import read_text, tweaker_root_match


def _entry_classification(ocr, windows_username=None):
    kind = ocr.get('classification', 'unknown') if ocr.get('ok') is True else 'unknown'
    if kind in ('BIOS', 'failure', 'windows'):
        return kind
    text = re.sub(r'\s+', ' ', str(ocr.get('text', '')).casefold()).strip()
    if ocr.get('ok') is True:
        # Welcome plus the known Windows account is stronger than a bare logo.
        if re.search(r'\bwelcome\b', text) and (
                'windows' in text or windows_username and windows_username.casefold() in text):
            return 'windows'
        if 'aorus' in text or 'gigabyte' in text:
            return 'logo'
    return 'unknown'


async def wait_video(client, run, poll_s=2, send_delete=False, *, windows_probe=None,
                     windows_username=None, classify_entry=None, unknown_observations=3):
    # Existing callers keep their behavior; firmware-UI entry explicitly asks
    # for BIOS/Windows classification while disabling all Delete requests.
    classify_entry = send_delete if classify_entry is None else classify_entry
    if run.load()['stage'] not in ('created', 'awaiting_bios_anchor', 'video_ready'):
        raise RuntimeError('Video entry cannot replace an active BIOS/review/save stage')
    stop = asyncio.Event()
    hid_stop = asyncio.Event()
    outcome = None
    last_image = None
    previous_hash = None
    previous_ocr = None
    ocr_attempt = 0
    ocr_evidence = None
    unknown_count = 0
    ocr_failures = 0
    stats = {'attempts': 0, 'api_successes': 0, 'api_failures': 0, 'api_cancelled': 0, 'error_types': {},
             'target_interval_ms': 250, 'meaning': 'API outcomes only, not target key delivery'}

    async def repeat_delete():
        nonlocal outcome
        while not stop.is_set() and not hid_stop.is_set():
            if (run.path / 'STOP').exists():
                outcome = {'status': 'stopped', 'image': last_image}
                stop.set()
                return
            start = asyncio.get_running_loop().time()
            stats['attempts'] += 1
            try:
                await client.keys(['Delete'])
                stats['api_successes'] += 1
            except asyncio.CancelledError:
                stats['api_cancelled'] += 1
                if (run.path / 'STOP').exists():
                    outcome = {'status': 'stopped', 'image': last_image}
                    stop.set()
                    return
                raise
            except Exception as error:
                stats['api_failures'] += 1
                kind = type(error).__name__
                stats['error_types'][kind] = stats['error_types'].get(kind, 0) + 1
            delay = max(0, .25 - (asyncio.get_running_loop().time() - start))
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except asyncio.TimeoutError:
                pass

    async def watch_stop():
        nonlocal outcome
        while not stop.is_set():
            if (run.path / 'STOP').exists():
                outcome = {'status': 'stopped', 'image': last_image}
                stop.set()
                return
            try:
                await asyncio.wait_for(stop.wait(), .25)
            except asyncio.TimeoutError:
                pass

    async def watch_windows():
        nonlocal outcome
        while not stop.is_set():
            evidence = await windows_probe()
            if evidence:
                outcome = {'status': 'missed_bios_entry', 'image': last_image,
                           'classification': 'windows', 'windows_evidence': evidence}
                stop.set()  # Stop HID even while OCR/screenshot is still pending.
                return
            try:
                await asyncio.wait_for(stop.wait(), max(1, poll_s))
            except asyncio.TimeoutError:
                pass

    async def observe(awaitable):
        operation = asyncio.create_task(awaitable)
        try:
            done, _ = await asyncio.wait((operation, stopped), return_when=asyncio.FIRST_COMPLETED)
            if stopped in done:
                return None
            return await operation
        finally:
            if not operation.done():
                operation.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await operation

    task = asyncio.create_task(repeat_delete()) if send_delete else None
    probe_task = asyncio.create_task(watch_windows()) if classify_entry and windows_probe else None
    stop_task = asyncio.create_task(watch_stop())
    stopped = asyncio.create_task(stop.wait())
    try:
        while True:  # deliberately no total POST timeout
            if (run.path / 'STOP').exists():
                outcome = {'status': 'stopped', 'image': last_image}
                break
            observation = await observe(client.screenshot())
            if observation is None:
                break
            picture, metadata = observation
            if picture:
                if not classify_entry:
                    item = run.image(picture, 'video_ready')
                    run.event('video_observed', image=item, metadata=metadata)
                    run.update(stage='video_ready')
                    return item
                digest = hashlib.sha256(picture).hexdigest()
                changed = digest != previous_hash
                if changed:
                    last_image = run.image(picture, 'entry_observed')
                    previous_hash = digest
                if changed or previous_ocr is None or previous_ocr.get('ok') is not True:
                    ocr = await observe(asyncio.to_thread(read_text, run.path / last_image['path']))
                    if ocr is None:
                        break
                    if (run.path / 'STOP').exists():
                        outcome = {'status': 'stopped', 'image': last_image}
                        break
                    previous_ocr = ocr
                    ocr_attempt += 1
                    ocr_evidence = last_image['path'] + f'.ocr-{ocr_attempt}.json'
                    evidence = run.path / ocr_evidence
                    evidence.write_text(json.dumps(ocr, ensure_ascii=False, indent=2), encoding='utf-8')
                else:
                    ocr = previous_ocr
                kind = _entry_classification(ocr, windows_username)
                if changed:
                    run.event('entry_frame_classified', image=last_image,
                              classification=kind, ocr_ok=ocr.get('ok') is True,
                              ocr_error=ocr.get('error'), metadata=metadata)
                if kind != 'logo':
                    hid_stop.set()  # Unknown may be a transient cursor; stop keys, keep observing.
                if kind in ('BIOS', 'failure'):
                    stop.set()
                    outcome = {'status': 'awaiting_bios_anchor' if kind == 'BIOS'
                               else 'boot_failure_detected', 'image': last_image,
                               'classification': kind, 'tweaker_root': tweaker_root_match(ocr),
                               'ocr_evidence': ocr_evidence}
                    break
                if kind == 'windows':
                    stop.set()
                    outcome = {'status': 'missed_bios_entry', 'image': last_image,
                               'classification': kind}
                    break
                if kind == 'unknown':
                    ocr_failures = ocr_failures + 1 if ocr.get('ok') is not True else 0
                    # Empty/punctuation-only boot cursors may persist while POST
                    # transitions. No total POST timer, and no more HID here.
                    meaningful = bool(re.search(r'[^\W_]', str(ocr.get('text', '')), re.UNICODE))
                    unknown_count = unknown_count + 1 if not changed and meaningful else int(meaningful)
                    if ocr_failures >= unknown_observations or unknown_count >= unknown_observations:
                        stop.set()
                        outcome = {'status': 'entry_unclassified', 'image': last_image,
                                   'classification': kind, 'ocr_error': ocr.get('error'),
                                   'reason': 'persistent_ocr_failure' if ocr_failures else 'stable_unrecognized_page',
                                   'ocr_evidence': ocr_evidence}
                        break
                else:
                    unknown_count = ocr_failures = 0
            else:
                run.event('no_video', metadata=metadata)
            try:
                await asyncio.wait_for(stop.wait(), poll_s)
            except asyncio.TimeoutError:
                pass
        if outcome is None:
            outcome = {'status': 'stopped', 'image': last_image}
        run.update(stage=outcome['status'])
        run.event(outcome['status'], **{k: v for k, v in outcome.items() if k != 'status'})
        return outcome
    finally:
        stop.set()
        for pending in (task, probe_task, stop_task, stopped):
            if pending:
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
        if send_delete:
            run.update(delete_api_stats=stats)
            run.event('delete_api_summary', **stats)


async def execute(client, run, actions):
    s = run.load()
    if s['stage'] != 'bios_anchor_confirmed':
        raise RuntimeError('Confirm the calibrated BIOS anchor before navigation')
    run.update(stage='navigating')
    try:
        for index, action in enumerate(actions):
            op = action['op']
            run.event('action_started', index=index, action=action)
            if op == 'key':
                for _ in range(action.get('count', 1)):
                    await client.keys(action['keys'])
                    await asyncio.sleep(action.get('interval_ms', 100) / 1000)
            elif op == 'type':
                await client.text(action['text'])
            elif op == 'wait':
                await asyncio.sleep(action.get('seconds', action.get('ms', 0) / 1000))
            elif op == 'capture':
                image, _ = await client.screenshot()
                if not image:
                    raise RuntimeError('Lost video during BIOS navigation')
                run.image(image, action.get('label', 'bios'))
            elif op == 'checkpoint':
                from .bios import expected_changes
                return run.review_request(expected_changes(s['profile'], s['slot']))
            else:
                raise ValueError('Unknown macro operation: ' + op)
            run.event('action_complete', index=index)
        raise RuntimeError('Macro ended without F10 checkpoint')
    except BaseException:
        # Cursor position after an interrupted macro is unknown. No resume-by-index.
        run.update(stage='navigation_interrupted')
        raise


def capture_warning(config, run, operation, error):
    """Durable warning, not an experiment verdict or a pre-boot gate."""
    run.event('dsview_' + operation + '_warning', error_type=type(error).__name__)
    from .notify import Notifier
    # Persist first; the independent watcher/worker drains the queue outbox.
    try:
        Notifier(config).notify(run, 'dsview_' + operation + '_warning',
                                'DSView ' + operation + ' failed; BIOS/POST/test control continues independently. Inspect capture output.', [], dry_run=True)
    except Exception as notification_error:
        run.event('capture_warning_queue_error',error_type=type(notification_error).__name__)


async def confirm_save(client, run, config=None):
    run.consume_approval()
    if config is not None:
        try:
            from .dsview import ensure_capture_started
            await asyncio.to_thread(ensure_capture_started, config, run)
        except Exception as error:
            capture_warning(config, run, 'start', error)
        try:
            from .hdmi_watch import start_background
            start_background(config, run)
        except Exception as error:
            run.event('hdmi_watcher_start_warning', error_type=type(error).__name__)
    sent_utc, sent_ns = utc(), time.monotonic_ns()
    await client.keys(['Enter'])
    ack_ns, ack_utc = time.monotonic_ns(), utc()
    from .boot_timing import begin
    begin(run, sent_utc, sent_ns, ack_utc, ack_ns)
    run.event('f10_confirmed', command_start_utc=sent_utc, ack_utc=ack_utc)
    run.update(stage='waiting_windows', save_sent_utc=sent_utc, save_ack_utc=ack_utc)
