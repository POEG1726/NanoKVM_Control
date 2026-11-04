"""Non-model event worker. Waits indefinitely; wakes the existing Codex thread."""
import argparse
import asyncio
import json
import subprocess
from pathlib import Path
import time
from .client import NativeMCP
from .host import Target, TargetUnavailable, telemetry_current, verify_vddp
from .bios import expected_changes
from .notify import Notifier, _run_lock
from .state import Run, utc
from .vision import read_text
from .transfers import ArtifactError, pull_test_artifacts
from . import boot_timing
from .hdmi_watch import start_background


ACTIVE_STAGES={'waiting_windows','artifacts_received','test_launch_requested','test_started'}


def require_post_save_stage(state):
    if state['stage'] not in ACTIVE_STAGES:
        raise RuntimeError('Worker may operate the target only after reviewed F10 confirmation')


async def final_notification(notify,run,event,summary,evidence=None):
    result=notify.notify(run,event,summary,evidence)
    while result['status'] in ('pending','deferred','busy'):
        if (run.path/'STOP').exists():
            return {'status':'stopped_pending_outbox'}
        await asyncio.sleep(30)
        result=notify.notify(run,event,summary,evidence)
    return result


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def record_desktop_ready(config, run, metadata):
    """Separate desktop evidence; capture stop runs in the independent watcher."""
    saved=run.load().get('save_sent_utc')
    if saved:
        from datetime import datetime
        boot=metadata.get('boot_id')
        if not boot or datetime.fromisoformat(boot.replace('Z','+00:00'))<=datetime.fromisoformat(saved.replace('Z','+00:00')):
            return False
    path = run.path / 'desktop-ready.json'
    if path.exists():
        return True
    stamp, ns = utc(), time.monotonic_ns()
    marker = {'observed_utc': stamp, 'observed_monotonic_ns': ns, 'evidence': metadata,
              'scope': 'Interactive desktop readiness observed by runner, not first HDMI output'}
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(marker, indent=2), encoding='utf-8')
    temporary.replace(path)
    try:
        boot_timing.desktop_ready(run, stamp, ns, metadata)
    except RuntimeError:
        run.event('desktop_timing_origin_unavailable')
    run.event('desktop_ready_observed', evidence=metadata)
    # A previous watcher may have exited; a fresh one can perform only CLI stop.
    try:
        start_background(config, run)
    except Exception as error:
        run.event('hdmi_watcher_start_warning', error_type=type(error).__name__)
    return True


def observe_target_desktop(target, run, config):
    output=target.ps("$p=@(Get-Process explorer -ErrorAction SilentlyContinue | Where-Object {$_.SessionId -gt 0}); if($p.Count){[pscustomobject]@{source='interactive_explorer_process_via_ssh';session_id=$p[0].SessionId;explorer_pid=$p[0].Id;boot_id=(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')} | ConvertTo-Json -Compress}")
    if output:
        return record_desktop_ready(config,run,json.loads(output))
    return False


def watcher_failure(run):
    path = run.path / 'hdmi-watch.json'
    if path.exists():
        observation = read(path)
        if observation.get('status') == 'boot_failure_detected':
            return observation
    return None


async def monitor(config, run, bootstrap=False, test='vt3'):
    target=Target(config); notify=Notifier(config)
    watcher_started=False
    active=run.load()['stage'] in ('test_launch_requested','test_started')
    if active and run.load().get('kind') != test:
        raise RuntimeError('Resume the already selected test kind')
    while True:
        if (run.path/'STOP').exists():
            run.event('worker_stopped'); return
        notify.drain(run)
        s=run.load()
        if s['stage'] in ('awaiting_f10_review','f10_approved'):
            notify.notify(run,'f10_review','Review complete F10 list and Profile8 agent-tune. Do not release without the hash-bound decision.',s['evidence'][-3:])
            return
        require_post_save_stage(s)
        if not watcher_started and config.get('_config_path'):
            try:
                start_background(config,run)
                watcher_started=True
            except Exception as error:
                run.event('hdmi_watcher_start_warning',error_type=type(error).__name__)
                notify.notify(run,'video_monitor_error','Independent HDMI watcher could not start; SSH/test control continues.',[],dry_run=True)
                watcher_started=True
        failure = watcher_failure(run)
        if failure:
            run.update(stage='boot_failure_detected')
            await final_notification(notify,run,'boot_failure','Explicit Boot Failure screen. Load Profile8 agent-tune, change only next authorized VDDP, save back to8, then request a NEW F10 review.',failure.get('evidence_path'))
            return
        try:
            if not (run.path/'desktop-ready.json').exists():
                observe_target_desktop(target,run,config)
            if not active:
                if bootstrap and not s.get('target_bootstrapped'):
                    target.deploy(Path(__file__).resolve().parents[1]/'target',run,start_capture=True)
                    run.update(target_bootstrapped=True)
                ready=target.fetch_ready(run)
                if ready:
                    manifest,local=ready
                    if s.get('save_sent_utc'):
                        from datetime import datetime
                        boot=datetime.fromisoformat(manifest['boot_id'].replace('Z','+00:00'))
                        saved=datetime.fromisoformat(s['save_sent_utc'].replace('Z','+00:00'))
                        if boot<=saved:
                            raise RuntimeError('Capture does not belong to a Windows boot after this F10 save')
                    record_desktop_ready(config,run,{'source':'completed_interactive_capture_manifest','boot_id':manifest.get('boot_id')})
                    v=expected_changes(run.load()['profile'],run.load()['slot'])[0]['to_mv']
                    readback=verify_vddp(manifest,local.parent,v,config.get('vddp_tolerance_v',.020),profile=run.load()['profile'])
                    obs=telemetry_current(config['telemetry_db'],manifest['hwtrace']['pid'],s['created_utc'])
                    if obs:
                        run.update(stage='artifacts_received',readback=readback,telemetry=obs)
                        target.receipt(run,manifest,local,obs,readback=readback)
                        result=json.loads(target.command('start-test',run,test))
                        if result.get('status')!='awaiting_receiver':
                            active=True
                            run.update(stage='test_launch_requested',kind=test,test_launch_requested_utc=utc())
            else:
                status=json.loads(target.command('status',run))
                result=status.get('test-'+test+'-result',status.get('test-result',{}))
                state=status.get('test',{})
                if (run.load()['stage']=='test_launch_requested'
                        and state.get('status') not in ('process_started',)
                        and result.get('status') not in ('running','process_started','finished','failed')):
                    from datetime import datetime,timezone
                    requested=run.load().get('test_launch_requested_utc')
                    if requested and (datetime.now(timezone.utc)-datetime.fromisoformat(requested)).total_seconds()>90:
                        raise RuntimeError('Interactive test task has no confirmed test process/result after 90 seconds; POST wait is unaffected')
                if state.get('status')=='process_started' or result.get('status') in ('running','process_started'):
                    if run.load()['stage']!='test_started':
                        run.update(stage='test_started',target_status=status,kind=test)
                        run.event('test_process_confirmed',kind=test)
                if result.get('status') in ('finished','failed'):
                    if test=='vt3':
                        observation_path=run.path/'test-vt3-status-observation.json'
                        observation_path.write_text(json.dumps(result,indent=2),encoding='utf-8')
                        partial=result.get('status')!='finished'
                        try:
                            receipt=await asyncio.to_thread(pull_test_artifacts,target,run,test,allow_partial=partial)
                        except ArtifactError as error:
                            run.update(stage='test_interrupted' if partial else 'test_artifact_error',result=result,artifact_error=str(error))
                            await final_notification(notify,run,'test_interrupted' if partial else 'test_artifact_error',
                                                     'VT3 ended but the complete requested artifact set could not be verified. Saved the status observation; do not claim ten logs received.',observation_path)
                            return
                        if receipt['complete_batch']:
                            run.update(stage='test_finished',result=result,test_artifacts_receipt=receipt)
                            await final_notification(notify,run,'test_finished','All ten VT3 runs and original logs/summary CSV+JSON were received locally and hash verified. Inspect results before selecting the next voltage.',receipt['receipt_path'])
                        else:
                            run.update(stage='test_interrupted',result=result,test_artifacts_receipt=receipt)
                            await final_notification(notify,run,'test_interrupted','VT3 batch was interrupted before verified completion of ten runs. Available diagnostic artifacts were received and hash verified; this is not a completed batch.',receipt['receipt_path'])
                    else:
                        path=run.path/('test-'+test+'-result.json')
                        path.write_text(json.dumps(result,indent=2),encoding='utf-8')
                        run.update(stage='test_finished',result=result)
                        await final_notification(notify,run,'test_finished','Test batch finished. Read result and logs before selecting the next voltage or starting long validation.',path)
                    return
                # TM5 returns after real process creation; its log remains the
                # error authority. Poll only the appended log tail for changes.
                if test=='tm5' and result.get('status')=='process_started':
                    from .host import quote
                    cfg=read(Path(__file__).resolve().parents[1]/'target/config.example.json')
                    log=Path(cfg['tests']['tm5']['working_directory'])/'Log.txt'
                    log=result.get('log_path',str(log))
                    offset=int(result.get('log_initial_size',0))
                    tail=target.ps(f"$p={quote(log)}; if(Test-Path -LiteralPath $p){{$b=[IO.File]::ReadAllBytes($p); $o={offset};if($b.Length -lt $o){{$o=0}};[Text.Encoding]::UTF8.GetString($b,$o,$b.Length-$o)}}")
                    if tail and tail!=s.get('tm5_log_tail'):
                        old=s.get('tm5_log_tail')
                        run.update(tm5_log_tail=tail)
                        if 'Error in test' in tail or 'Testing stopped' in tail:
                            path=run.path/'tm5-log-tail.txt';path.write_text(tail,encoding='utf-8')
                            notify.notify(run,'tm5_log_changed','TM5 error or stop log changed. Evaluate current run; do not infer errors from WHEA.',path)
                    if status.get('test_process_alive') is False:
                        run.update(stage='test_finished',result={'status':'process_exit_observed','cause':'unknown'})
                        await final_notification(notify,run,'tm5_process_exited','TM5 process exit observed; read the current log to determine outcome and cause.',[])
                        return
        except TargetUnavailable:
            pass  # POST/Windows start has no total timeout.
        except (subprocess.TimeoutExpired,TimeoutError):
            pass
        except Exception as error:
            run.update(stage='needs_attention',worker_error=type(error).__name__+': '+str(error))
            await final_notification(notify,run,'worker_failure','Pipeline stopped for inspection; no retry of HID/save/test launch. Error class: '+type(error).__name__,[])
            return
        await asyncio.sleep(config.get('worker_poll_seconds',5))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',default='config/host.json');p.add_argument('--run',required=True)
    p.add_argument('--bootstrap',action='store_true');p.add_argument('--test',choices=('vt3','tm5'),default='vt3')
    a=p.parse_args();c=read(a.config);c['_config_path']=str(Path(a.config).resolve());r=Run(c['runs_root'],a.run)
    r.event('worker_started')
    with _run_lock(r.path/'worker.lock'):
        asyncio.run(monitor(c,r,a.bootstrap,a.test))


if __name__=='__main__':main()
