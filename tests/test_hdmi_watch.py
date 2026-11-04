import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from nanokvm_flow import boot_timing
from nanokvm_flow.hdmi_watch import start_background, watch
from nanokvm_flow.state import Run, utc
from nanokvm_flow.worker import record_desktop_ready


class HdmiWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.run=Run(self.tmp.name,'video');self.run.create({},0)
        ns=time.monotonic_ns();now=utc();boot_timing.begin(self.run,now,ns,now,ns)

    def link_capture(self):
        directory=str(self.run.path/'capture')
        (self.run.path/'dsview-start.json').write_text(json.dumps({'state':{'output_directory':directory}}))
        return directory

    def test_watcher_is_an_independent_hidden_process(self):
        config={'_config_path':str(Path(self.tmp.name)/'host.json')}
        with patch('nanokvm_flow.hdmi_watch.subprocess.Popen') as proc:
            proc.return_value.pid=54321
            result=start_background(config,self.run)
        self.assertEqual(result['status'],'requested')
        self.assertIn('nanokvm_flow.hdmi_watch',proc.call_args.args[0])
        self.assertIn('creationflags',proc.call_args.kwargs)

    def test_stop_marker_prevents_spawning(self):
        (self.run.path/'STOP').write_text('stop')
        with patch('nanokvm_flow.hdmi_watch.subprocess.Popen') as proc:
            self.assertEqual(start_background({'_config_path':'fixture.json'},self.run)['status'],'stopped')
            proc.assert_not_called()

    def test_restore_timing_is_written_and_desktop_stop_is_separate(self):
        directory=self.link_capture()
        images=iter([(None,{}),(b'fixture-image',{})])
        class Client:
            def __init__(self,config):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def screenshot(self):return next(images)
        def ocr(path):
            (self.run.path/'desktop-ready.json').write_text('{}')
            return {'classification':'unknown'}
        with patch('nanokvm_flow.hdmi_watch.Notifier') as notify, patch('nanokvm_flow.dsview.command',return_value={'output_directory':directory}), patch('nanokvm_flow.dsview.stop_capture',return_value={'status':'stopped'}) as stop:
            asyncio.run(watch({'hdmi_poll_seconds':0},self.run,client_factory=Client,ocr=ocr))
            stop.assert_called_once()
        timing=json.loads((self.run.path/'boot-timing.json').read_text())
        self.assertIsNotNone(timing['endpoints']['first_signal_restored'])
        self.assertIsNone(timing['endpoints']['desktop_ready'])
        self.assertEqual(json.loads((self.run.path/'hdmi-watch.json').read_text())['status'],'desktop_ready')
        self.assertEqual(self.run.load()['stage'],'created')

    def test_explicit_ocr_failure_is_only_a_report_not_hid_or_run_state_change(self):
        class Client:
            def __init__(self,config):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def screenshot(self):return b'failure-picture',{}
        with patch('nanokvm_flow.hdmi_watch.Notifier'):
            asyncio.run(watch({},self.run,client_factory=Client,ocr=lambda _: {'classification':'failure'}))
        status=json.loads((self.run.path/'hdmi-watch.json').read_text())
        self.assertEqual(status['status'],'boot_failure_detected')
        self.assertTrue(Path(status['evidence_path']).is_file())
        self.assertEqual(self.run.load()['stage'],'created')

    def test_stop_capture_failure_is_independent_warning(self):
        directory=self.link_capture()
        (self.run.path/'desktop-ready.json').write_text('{}')
        with patch('nanokvm_flow.hdmi_watch.Notifier'), patch('nanokvm_flow.dsview.command',return_value={'output_directory':directory}), patch('nanokvm_flow.dsview.stop_capture',side_effect=RuntimeError('fixture flush failed')):
            asyncio.run(watch({'notifications':{}},self.run))
        self.assertEqual(self.run.load()['stage'],'created')
        self.assertEqual(json.loads((self.run.path/'dsview-stop-attempt.json').read_text())['status'],'failed')
        self.assertEqual(json.loads((self.run.path/'hdmi-watch.json').read_text())['status'],'desktop_ready')

    def test_legacy_run_without_capture_receipt_never_stops_dsview(self):
        (self.run.path/'desktop-ready.json').write_text('{}')
        with patch('nanokvm_flow.hdmi_watch.Notifier'), patch('nanokvm_flow.dsview.command') as command, patch('nanokvm_flow.dsview.stop_capture') as stop:
            asyncio.run(watch({},self.run))
        command.assert_not_called();stop.assert_not_called()
        self.assertEqual(json.loads((self.run.path/'dsview-stop-attempt.json').read_text())['status'],'skipped_not_started_by_run')

    def test_changed_recording_is_left_untouched(self):
        self.link_capture()
        (self.run.path/'desktop-ready.json').write_text('{}')
        with patch('nanokvm_flow.hdmi_watch.Notifier'), patch('nanokvm_flow.dsview.command',return_value={'output_directory':str(self.run.path/'other-capture')}), patch('nanokvm_flow.dsview.stop_capture') as stop:
            asyncio.run(watch({'notifications':{}},self.run))
        stop.assert_not_called()
        self.assertEqual(json.loads((self.run.path/'dsview-stop-attempt.json').read_text())['status'],'failed')

    def test_async_start_can_resolve_to_its_unique_recording_subdirectory(self):
        root=self.run.path/'capture'
        (self.run.path/'dsview-start.json').write_text(json.dumps({'state':{'output_directory':''},'output_directory':str(root)}))
        (self.run.path/'desktop-ready.json').write_text('{}')
        with patch('nanokvm_flow.hdmi_watch.Notifier'), patch('nanokvm_flow.dsview.command',return_value={'output_directory':str(root/'DSView-loop-segment')}), patch('nanokvm_flow.dsview.stop_capture',return_value={'status':'stopped'}) as stop:
            asyncio.run(watch({},self.run))
        stop.assert_called_once()

    def test_desktop_marker_does_not_wait_for_dsview_flush_in_ssh_worker(self):
        with patch('nanokvm_flow.worker.start_background'), patch('nanokvm_flow.dsview.stop_capture') as stop:
            record_desktop_ready({},self.run,{'source':'fixture-interactive-shell'})
            stop.assert_not_called()
        timing=json.loads((self.run.path/'boot-timing.json').read_text())
        self.assertIsNotNone(timing['endpoints']['desktop_ready'])
        self.assertIsNone(timing['endpoints']['first_signal_restored'])


if __name__=='__main__':unittest.main()
