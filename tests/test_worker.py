import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from nanokvm_flow.client import NativeMCP
from nanokvm_flow.host import verify_vddp
from nanokvm_flow.state import Run
from nanokvm_flow.worker import monitor, require_post_save_stage, record_desktop_ready
from nanokvm_flow.transfers import ArtifactError


class WorkerTests(unittest.TestCase):
    def test_unreviewed_stages_cannot_operate_target(self):
        for stage in ('created','bios_anchor_confirmed','navigation_interrupted','needs_attention','boot_failure_detected'):
            with self.assertRaises(RuntimeError): require_post_save_stage({'stage':stage})

    def test_created_run_cannot_bootstrap_or_start_test(self):
        with tempfile.TemporaryDirectory() as temp:
            run=Run(temp,'x');run.create({},0)
            with patch('nanokvm_flow.worker.Target') as target:
                with self.assertRaises(RuntimeError):asyncio.run(monitor({'notifications':{}},run,bootstrap=True))
                target.return_value.deploy.assert_not_called()
                target.return_value.command.assert_not_called()

    def test_launch_requested_resumes_monitor_without_duplicate_start(self):
        with tempfile.TemporaryDirectory() as temp:
            run=Run(temp,'x');run.create({},0);run.update(stage='test_launch_requested',kind='vt3')
            result={'test-vt3-result':{'status':'finished','runs':[],'passed':False}}
            receipt={'complete_batch':True,'run_count':10,'receipt_path':str(run.path/'received-tests.json')}
            (run.path/'received-tests.json').write_text(json.dumps(receipt))
            with patch('nanokvm_flow.worker.Target') as target, patch('nanokvm_flow.worker.pull_test_artifacts',return_value=receipt) as pull:
                target.return_value.command.return_value=json.dumps(result)
                target.return_value.ps.return_value=''
                asyncio.run(monitor({'notifications':{}},run))
                self.assertEqual(target.return_value.command.call_args.args[0],'status')
                self.assertEqual(run.load()['stage'],'test_finished')
                pull.assert_called_once()

    def test_old_or_unknown_boot_cannot_mark_desktop_ready(self):
        with tempfile.TemporaryDirectory() as temp:
            run=Run(temp,'desktop');run.create({},0)
            run.update(save_sent_utc='2026-11-04T05:07:07+00:00')
            with patch('nanokvm_flow.worker.start_background'):
                for boot in (None,'2026-11-04T05:00:00Z','2026-11-04T05:07:07Z'):
                    self.assertFalse(record_desktop_ready({},run,{'boot_id':boot}))
                    self.assertFalse((run.path/'desktop-ready.json').exists())
                self.assertTrue(record_desktop_ready({},run,{'boot_id':'2026-11-04T05:20:00Z'}))
                self.assertTrue((run.path/'desktop-ready.json').is_file())
                self.assertFalse((run.path/'desktop-ready.tmp').exists())

    def test_native_capture_discards_observed_stale_frame(self):
        client=NativeMCP({})
        class Session:
            def __init__(self):self.index=0
            async def call_tool(self,*args):
                self.index+=1
                data=b'old' if self.index==1 else b'current'
                return SimpleNamespace(content=[SimpleNamespace(type='image',data=base64.b64encode(data))])
        client.session=Session()
        picture,_=asyncio.run(client.screenshot())
        self.assertEqual(picture,b'current')

    def test_vddp_report_must_match_and_be_finite(self):
        with tempfile.TemporaryDirectory() as temp:
            file=Path(temp)/'zen.json';manifest={'files':[{'name':'zen.json'}]}
            for v in (.9472,float('nan')):
                file.write_text(json.dumps({'readings':{'power_table':{'cldo_vddp_v':v}}}))
                with self.assertRaises(RuntimeError):verify_vddp(manifest,temp,900)
            file.write_text(json.dumps({'readings':{'power_table':{'cldo_vddp_v':.9}}}))
            self.assertEqual(verify_vddp(manifest,temp,900)['reported_v'],.9)

    def _terminal(self, temp, status='finished', receipt=None, transfer_error=None):
        run=Run(temp,'batch');run.create({},0);run.update(stage='test_started',kind='vt3')
        path=run.path/'fixture-receipt.json';path.write_text('{}')
        receipt=receipt or {'complete_batch':True,'run_count':10,'receipt_path':str(path)}
        order=[]
        def pulled(*args,**kwargs):
            order.append('artifacts')
            if transfer_error:raise transfer_error
            return receipt
        def notified(run,event,*args,**kwargs):
            order.append(event);return {'status':'queued'}
        with patch('nanokvm_flow.worker.Target') as target, patch('nanokvm_flow.worker.Notifier') as notify, patch('nanokvm_flow.worker.pull_test_artifacts',side_effect=pulled) as pull:
            target.return_value.ps.return_value=''
            target.return_value.command.return_value=json.dumps({'test-vt3-result':{'status':status,'runs':[]}})
            notify.return_value.notify.side_effect=notified
            asyncio.run(monitor({'notifications':{}},run))
            kwargs=pull.call_args.kwargs
        return run,order,kwargs

    def test_completion_queue_follows_verified_download(self):
        with tempfile.TemporaryDirectory() as temp:
            run,order,kwargs=self._terminal(temp)
            self.assertEqual(order,['artifacts','test_finished'])
            self.assertEqual(run.load()['stage'],'test_finished')
            self.assertFalse(kwargs['allow_partial'])

    def test_bad_artifacts_never_emit_finished(self):
        with tempfile.TemporaryDirectory() as temp:
            run,order,_=self._terminal(temp,transfer_error=ArtifactError('fixture hash mismatch'))
            self.assertEqual(order,['artifacts','test_artifact_error'])
            self.assertEqual(run.load()['stage'],'test_artifact_error')
            self.assertTrue((run.path/'test-vt3-status-observation.json').exists())

    def test_partial_batch_has_distinct_stage_and_notification(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'partial.json';path.write_text('{}')
            receipt={'complete_batch':False,'run_count':2,'receipt_path':str(path)}
            run,order,kwargs=self._terminal(temp,status='failed',receipt=receipt)
            self.assertEqual(order,['artifacts','test_interrupted'])
            self.assertEqual(run.load()['stage'],'test_interrupted')
            self.assertTrue(kwargs['allow_partial'])


if __name__=='__main__':unittest.main()
