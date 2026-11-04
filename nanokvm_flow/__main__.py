import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
import subprocess
from .bios import build_actions, expected_changes, validate_profile
from .client import NativeMCP
from .host import Target, TargetUnavailable, telemetry_current, verify_vddp
from .state import Run, sha
from .workflow import execute, wait_video, confirm_save


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


async def hardware(args, run):
    async with NativeMCP(args.config) as client:
        if args.command == 'enter':
            return await wait_video(client, run, send_delete=True)
        if args.command == 'wait-video':
            return await wait_video(client, run)
        if args.command == 'shot':
            picture, metadata = await client.screenshot()
            if not picture:
                return {'video': False, 'metadata': metadata}
            item=run.image(picture, args.label)
            if run.load()['stage']=='awaiting_f10_review' and args.label.startswith('f10'):
                s=run.load()
                run.review_request(expected_changes(s['profile'],s['slot']))
            return item
        if args.command == 'keys':
            state = run.load()
            if state['stage'] in ('awaiting_f10_review', 'f10_approved', 'save_confirmation_sent'):
                raise RuntimeError('Generic HID disabled at the save-review boundary')
            allowed = {'ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Home','End','PageUp','PageDown','Escape','Enter','F2','Delete','Tab'}
            if args.key not in allowed or not 1 <= args.count <= 100:
                raise ValueError('Unsupported calibration keys')
            for _ in range(args.count):
                await client.keys([args.key]); await asyncio.sleep(.1)
            run.event('calibration_keys', key=args.key, count=args.count)
            # Any navigation invalidates a previously confirmed screen anchor.
            run.update(stage='awaiting_bios_anchor')
            await asyncio.sleep(2)
            picture, _ = await client.screenshot()
            if picture:
                return run.image(picture, 'calibration')
        if args.command == 'type-value':
            if run.load()['stage'] not in ('created','awaiting_bios_anchor'):
                raise RuntimeError('Value calibration only before review/navigation')
            if args.value not in ('0.900','0.950','1.000','1.050','1.100'):
                raise ValueError('Only authorized VDDP candidates may be typed')
            await client.text(args.value)
            run.event('calibration_value_typed',value=args.value)
            picture,_=await client.screenshot()
            if picture:
                return run.image(picture,'typed_value')
        if args.command == 'profile-name':
            if run.load()['stage'] != 'awaiting_bios_anchor' or args.name != 'agent-tune':
                raise RuntimeError('Only the user-authorized Profile8 name agent-tune is allowed')
            await client.text(args.name)
            run.event('bios_profile_name_typed',slot=8,name=args.name)
            await asyncio.sleep(2)
            picture,_=await client.screenshot()
            if picture:
                return run.image(picture,'profile8_named')
        if args.command == 'review-page':
            if run.load()['stage'] != 'awaiting_f10_review':
                raise RuntimeError('Only scroll the pending F10 review')
            await client.keys([args.key]); await asyncio.sleep(.2)
            picture,_=await client.screenshot()
            if not picture:
                raise RuntimeError('Missing F10 review-page video')
            run.image(picture,'f10_page')
            s=run.load()
            return run.review_request(expected_changes(s['profile'],s['slot']))
        if args.command == 'cancel-review':
            if run.load()['stage'] not in ('awaiting_f10_review','f10_approved'):
                raise RuntimeError('No pending save review to cancel')
            run.event('f10_review_revoked',reason='User requested BIOS profile save before exit')
            await client.keys(['Escape'])
            run.update(stage='awaiting_bios_anchor',approved_request=None)
            await asyncio.sleep(2)
            picture,_=await client.screenshot()
            if picture:
                return run.image(picture,'f10_cancelled_for_profile')
        if args.command == 'apply':
            return await execute(client, run, build_actions(run.load()['profile'], run.load()['slot']))
        if args.command == 'save':
            return await confirm_save(client, run, args.config)


def main():
    parser = argparse.ArgumentParser(description='Direct scripted NanoKVM. No gateway and no total POST timeout.')
    parser.add_argument('--config', default='config/host.json')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('init','status','enter','wait-video','shot','keys','anchor','calibrate','apply','review','save','arm','postboot','deploy','review-page','type-value','cancel-review','profile-name','monitor'):
        p = sub.add_parser(name); p.add_argument('--run', required=True)
        if name == 'init':
            p.add_argument('--profile', default='profiles/b850m_f12d.json'); p.add_argument('--slot', type=int, default=0)
        if name == 'shot':
            p.add_argument('--label', default='screen')
        if name == 'keys':
            p.add_argument('--key', required=True); p.add_argument('--count', type=int, default=1)
        if name == 'review-page':
            p.add_argument('--key', choices=('PageUp','PageDown','ArrowUp','ArrowDown'),default='PageDown')
        if name == 'type-value':
            p.add_argument('--value',required=True)
        if name == 'profile-name':
            p.add_argument('--name',required=True)
        if name == 'anchor':
            p.add_argument('--image-sha256', required=True)
        if name == 'calibrate':
            p.add_argument('--profile', required=True)
        if name == 'review':
            p.add_argument('--decision', required=True)
        if name == 'postboot':
            p.add_argument('--kind', choices=('vt3','tm5'), default='vt3')
        if name == 'monitor':
            p.add_argument('--kind',choices=('vt3','tm5'),default='vt3')
            p.add_argument('--bootstrap',action='store_true')
            p.add_argument('--background',action='store_true')
    args = parser.parse_args(); args.config_path=str(Path(args.config).resolve());args.config = read(args.config)
    args.config['_config_path']=args.config_path
    run = Run(args.config['runs_root'], args.run)
    if args.command == 'init':
        profile = read(args.profile); validate_profile(profile); expected_changes(profile,args.slot)
        run.create(profile,args.slot); result = run.load()
    elif args.command == 'status':
        result = run.load()
    elif args.command == 'calibrate':
        if run.load()['stage'] not in ('created','awaiting_bios_anchor'):
            raise RuntimeError('Calibration only before navigation')
        profile = read(args.profile); validate_profile(profile)
        if not profile['calibration']['verified']:
            raise RuntimeError('Calibration must reference actual screen evidence')
        result = run.update(profile=profile,stage='awaiting_bios_anchor')
    elif args.command == 'anchor':
        s = run.load()
        if s['stage'] not in ('created','awaiting_bios_anchor') or not s['evidence']:
            raise RuntimeError('Need a current BIOS anchor image')
        last = s['evidence'][-1]
        if args.image_sha256 != last['sha256'] or sha(run.path/last['path']) != last['sha256']:
            raise RuntimeError('Wrong anchor image')
        result = run.update(stage='bios_anchor_confirmed',anchor_image=last)
    elif args.command == 'review':
        run.approve(read(args.decision)); result={'stage':run.load()['stage']}
    elif args.command == 'arm':
        Target(args.config).arm(run); result={'armed':run.name}
    elif args.command == 'monitor':
        command=[sys.executable,'-m','nanokvm_flow.worker','--config',str(Path(parser.get_default('config')).resolve()),'--run',run.name,'--test',args.kind]
        # Preserve an explicitly supplied config path, not just argparse default.
        config_arg=sys.argv[sys.argv.index('--config')+1] if '--config' in sys.argv else parser.get_default('config')
        command[command.index('--config')+1]=str(Path(config_arg).resolve())
        if args.bootstrap:command.append('--bootstrap')
        if args.background:
            with (run.path/'worker.log').open('ab') as log:
                proc=subprocess.Popen(command,cwd=Path(__file__).resolve().parents[1],stdout=log,stderr=subprocess.STDOUT,
                                      creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            run.update(worker_pid=proc.pid)
            result={'worker_pid':proc.pid,'mode':'background','run_id':run.name}
        else:
            subprocess.run(command,check=True);result={'worker_finished':True}
    elif args.command == 'deploy':
        target=Target(args.config)
        root=Path(__file__).resolve().parent.parent/'target'
        result=target.deploy(root,run,start_capture=False)
    elif args.command == 'postboot':
        if run.load()['stage'] not in ('waiting_windows','artifacts_received'):
            raise RuntimeError('Run must have passed the F10 review/save boundary')
        target = Target(args.config)
        from .worker import record_desktop_ready, observe_target_desktop
        from .hdmi_watch import start_background
        try:
            start_background(args.config,run)
        except Exception as error:
            run.event('hdmi_watcher_start_warning',error_type=type(error).__name__)
        while True:
            try:
                if not (run.path/'desktop-ready.json').exists():
                    observe_target_desktop(target,run,args.config)
                ready = target.fetch_ready(run)
            except (TargetUnavailable, TimeoutError):
                time.sleep(5); continue
            if ready:
                manifest, manifest_file = ready
                if not record_desktop_ready(args.config,run,{'source':'completed_interactive_capture_manifest','boot_id':manifest.get('boot_id')}):
                    raise RuntimeError('Capture does not belong to a Windows boot after this F10 save')
                expected = expected_changes(run.load()['profile'],run.load()['slot'])[0]['to_mv']
                readback = verify_vddp(manifest,manifest_file.parent,expected,args.config.get('vddp_tolerance_v',.020),profile=run.load()['profile'])
                trace = manifest.get('hwtrace',manifest.get('collector',{}))
                observation = telemetry_current(args.config['telemetry_db'],trace['pid'],run.load()['created_utc'])
                if observation:
                    run.update(stage='artifacts_received',readback=readback,telemetry=observation)
                    target.receipt(run,manifest,manifest_file,observation,readback=readback)
                    result=target.command('start-test',run,args.kind)
                    requested=json.loads(result)
                    if requested.get('status') == 'awaiting_receiver':
                        run.event('collector_restarted_requires_new_receipt',pid=requested.get('hwtrace',{}).get('pid'))
                        time.sleep(2)
                        continue
                    run.update(stage='test_launch_requested',kind=args.kind)
                    for _ in range(30):
                        status=json.loads(target.command('status',run))
                        launched=status.get('test',{})
                        outcome=status.get('test-result',{})
                        if launched.get('status')=='process_started' or outcome.get('status') in ('process_started','running','finished'):
                            run.update(stage='test_started',kind=args.kind,target_status=status)
                            result=status
                            break
                        if outcome.get('status')=='failed':
                            raise RuntimeError('Target test controller failed')
                        time.sleep(1)
                    else:
                        raise RuntimeError('Launch requested but no test process/result confirmed')
                    break
            time.sleep(5)
    else:
        result=asyncio.run(hardware(args,run))
        if args.command=='apply' and run.load()['stage']=='awaiting_f10_review':
            from .notify import Notifier
            Notifier(args.config).notify(run,'f10_review','Script paused. Review latest complete F10 change list and Profile8 agent-tune before approving.',run.load()['evidence'][-3:])
    if isinstance(result,dict) and 'profile' in result:
        result={k:result[k] for k in ('run_id','stage','slot')}
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        # Avoid HTTP headers/token-bearing SDK tracebacks on the public console.
        print(f'{type(exc).__name__}: {exc}' if type(exc).__module__=='builtins' else type(exc).__name__,file=sys.stderr)
        sys.exit(1)
