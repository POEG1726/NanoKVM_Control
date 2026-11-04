"""Firmware-UI BIOS classification without HID; entirely offline fakes."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from nanokvm_flow.boot_entry import enter_bios
from nanokvm_flow.state import Run
from nanokvm_flow.workflow import wait_video


class FirmwareUIEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Run(self.temp.name, 'firmware-entry')
        self.run.create({}, 0)
        self.pressed = []
        self.frames = [b'old-windows', None, b'AORUS', b'Tweaker Advanced Mode']
        owner = self

        class Client:
            def __init__(self, config=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def screenshot(self):
                await asyncio.sleep(0)
                return (owner.frames.pop(0) if len(owner.frames)>1 else owner.frames[0]), {}

            async def keys(self, keys):
                owner.pressed.append(keys)

        self.client = Client
        self.target = Mock()

        def ps(command):
            if 'shutdown.exe' in command:
                return '{"requested":true,"exit_code":0}'
            if 'Get-Process explorer' in command:
                return ''
            return '{"stress":[]}'

        self.target.ps.side_effect = ps

    @staticmethod
    def ocr(path):
        text = Path(path).read_bytes().decode()
        kind = ('BIOS' if 'Tweaker' in text else 'failure' if 'Boot Failure' in text
                else 'windows' if 'Windows sign-in' in text else 'unknown')
        return {'ok': True, 'classification': kind, 'text': text}

    def execute(self, *, firmware_ui=True):
        with patch('nanokvm_flow.boot_entry.ensure_capture_started'), \
             patch('nanokvm_flow.boot_entry.Notifier') as notifier, \
             patch('nanokvm_flow.workflow.read_text', side_effect=self.ocr):
            notifier.return_value.notify.return_value = {'status': 'queued'}
            return asyncio.run(enter_bios({}, self.run, target=self.target,
                                         client_factory=self.client, poll_s=0,
                                         firmware_ui=firmware_ui))

    def classifications(self):
        events = [json.loads(line) for line in (self.run.path/'events.jsonl').read_text().splitlines()]
        return [e['classification'] for e in events if e['event']=='entry_frame_classified']

    def test_firmware_logo_then_bios_is_classified_without_any_keys(self):
        result = self.execute()
        self.assertEqual(result['status'], 'awaiting_bios_anchor')
        self.assertEqual(self.classifications(), ['logo', 'BIOS'])
        self.assertEqual(self.pressed, [])
        self.assertNotIn('delete_api_stats', self.run.load())
        restart = [call.args[0] for call in self.target.ps.call_args_list if 'shutdown.exe' in call.args[0]]
        self.assertEqual(len(restart), 1)
        self.assertIn('/r /fw /t 0', restart[0])

    def test_firmware_windows_screen_is_missed_without_any_keys_or_restart_retry(self):
        self.frames = [b'old-windows', None, b'AORUS', b'Windows sign-in options']
        result = self.execute()
        self.assertEqual(result['status'], 'missed_bios_entry')
        self.assertEqual(self.pressed, [])
        self.assertEqual(self.classifications(), ['logo', 'windows'])
        self.assertEqual(sum('shutdown.exe' in call.args[0] for call in self.target.ps.call_args_list), 1)

    def test_firmware_boot_failure_and_unknown_still_pause_without_keys(self):
        for picture, expected in ((b'Boot Failure Detected', 'boot_failure_detected'),
                                  (b'unrecognized display', 'entry_unclassified')):
            with self.subTest(picture=picture):
                self.run.update(stage='created')
                receipt = self.run.path/'restart-request.json'
                if receipt.exists():
                    receipt.unlink()
                self.frames = [b'old-windows', None, picture]
                result = self.execute()
                self.assertEqual(result['status'], expected)
                self.assertEqual(self.pressed, [])

    def test_ordinary_entry_keeps_delete_requests(self):
        result = self.execute(firmware_ui=False)
        self.assertEqual(result['status'], 'awaiting_bios_anchor')
        self.assertTrue(self.pressed)
        self.assertTrue(all(keys==['Delete'] for keys in self.pressed))
        self.assertEqual(self.run.load()['delete_api_stats']['target_interval_ms'], 250)

    def test_firmware_new_boot_explorer_fallback_runs_without_hid(self):
        original = self.target.ps.side_effect

        def ps(command):
            if 'Get-Process explorer' in command:
                return '{"explorer_pid":42,"session_id":1,"boot_id":"2999-01-01T00:00:00Z"}'
            return original(command)

        self.target.ps.side_effect = ps
        self.frames = [b'old-windows', None] + [b'AORUS']*100
        result = self.execute()
        self.assertEqual(result['status'], 'missed_bios_entry')
        self.assertEqual(result['windows_evidence']['explorer_pid'], 42)
        self.assertEqual(self.pressed, [])

    def test_firmware_old_boot_explorer_is_ignored_without_hid(self):
        original = self.target.ps.side_effect

        def ps(command):
            if 'Get-Process explorer' in command:
                return '{"explorer_pid":42,"session_id":1,"boot_id":"2000-01-01T00:00:00Z"}'
            return original(command)

        self.target.ps.side_effect = ps
        result = self.execute()
        self.assertEqual(result['status'], 'awaiting_bios_anchor')
        self.assertNotIn('windows_evidence', result)
        self.assertEqual(self.pressed, [])

    def test_plain_wait_video_keeps_its_unclassified_no_hid_contract(self):
        self.frames = [b'Windows sign-in options']
        with patch('nanokvm_flow.workflow.read_text') as ocr:
            image = asyncio.run(wait_video(self.client(), self.run, poll_s=0))
        self.assertIn('path', image)
        self.assertEqual(self.run.load()['stage'], 'video_ready')
        self.assertEqual(self.pressed, [])
        ocr.assert_not_called()

    def test_no_hid_entry_stop_interrupts_a_pending_frame(self):
        owner = self

        class PendingClient:
            async def screenshot(self):
                await asyncio.Event().wait()

            async def keys(self, keys):
                owner.fail('Firmware entry must not send keys')

        async def exercise():
            async def stop_soon():
                await asyncio.sleep(.02)
                (owner.run.path/'STOP').write_text('stop')
            trigger = asyncio.create_task(stop_soon())
            try:
                return await asyncio.wait_for(wait_video(PendingClient(), owner.run,
                    poll_s=.001, send_delete=False, classify_entry=True), 1)
            finally:
                await trigger

        result = asyncio.run(exercise())
        self.assertEqual(result['status'], 'stopped')


if __name__=='__main__':
    unittest.main()
