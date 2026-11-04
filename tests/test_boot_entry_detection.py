"""Offline BIOS-entry frames/HID fakes; no NanoKVM, SSH or Windows OCR access."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from nanokvm_flow.state import Run
from nanokvm_flow.workflow import wait_video, _entry_classification


class EntryDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Run(self.tmp.name, 'entry')
        self.run.create({}, 0)
        self.keys = []

    def events(self):
        return [json.loads(line) for line in (self.run.path / 'events.jsonl').read_text().splitlines()]

    def client(self, frames, delay=0):
        owner = self
        class Client:
            async def screenshot(self):
                await asyncio.sleep(delay)
                return (frames.pop(0) if len(frames)>1 else frames[0]), {}

            async def keys(self, keys):
                owner.keys.append((time.monotonic(), keys))
        return Client()

    @staticmethod
    def ocr(path):
        text = Path(path).read_bytes().decode()
        classification = 'BIOS' if 'Tweaker' in text else 'failure' if 'Boot Failure' in text else 'unknown'
        return {'ok': True, 'classification': classification, 'text': text, 'error': None}

    def execute(self, client, **kwargs):
        with patch('nanokvm_flow.workflow.read_text', side_effect=self.ocr):
            return asyncio.run(wait_video(client, self.run, poll_s=.001, send_delete=True, **kwargs))

    def test_logo_is_not_bios_and_del_continues_until_bios_text(self):
        client = self.client([b'AORUS', b'Tweaker Advanced Mode Save & Exit'], delay=.28)
        result = self.execute(client)
        self.assertEqual(result['status'], 'awaiting_bios_anchor')
        classifications = [e['classification'] for e in self.events() if e['event'] == 'entry_frame_classified']
        self.assertEqual(classifications, ['logo', 'BIOS'])
        self.assertGreaterEqual(len(self.keys), 3)
        self.assertTrue(all(k == ['Delete'] for _, k in self.keys))
        self.assertEqual(self.run.load()['delete_api_stats']['api_failures'], 0)

    def test_logo_then_account_welcome_is_missed_entry_not_bios(self):
        result = self.execute(self.client([b'AORUS', b'POEG1726 Welcome']), windows_username='POEG1726')
        self.assertEqual(result['status'], 'missed_bios_entry')
        self.assertEqual(self.run.load()['stage'], 'missed_bios_entry')
        self.assertFalse(any(e['event'] == 'awaiting_bios_anchor' for e in self.events()))

    def test_explicit_boot_failure_stops_for_inspection(self):
        result = self.execute(self.client([b'AORUS', b'Boot Failure Detected']))
        self.assertEqual(result['status'], 'boot_failure_detected')
        self.assertTrue(all(k == ['Delete'] for _, k in self.keys))

    def test_transient_hid_errors_continue_at_250ms_with_api_only_counts(self):
        owner = self
        class Client:
            async def screenshot(self):
                await asyncio.sleep(.68)
                return b'Tweaker Advanced Mode', {}

            async def keys(self, keys):
                owner.keys.append((time.monotonic(), keys))
                if len(owner.keys) <= 2:
                    raise RuntimeError('transient hidg0 fixture')
        result = self.execute(Client())
        self.assertEqual(result['status'], 'awaiting_bios_anchor')
        stats = self.run.load()['delete_api_stats']
        self.assertEqual(stats['api_failures'], 2)
        self.assertGreaterEqual(stats['api_successes'], 1)
        self.assertIn('not target key delivery', stats['meaning'])
        self.assertTrue(all(b[0] - a[0] >= .20 for a, b in zip(self.keys, self.keys[1:])))

    def test_no_hdmi_has_no_total_timeout_and_user_stop_ends_keys(self):
        owner = self
        class Client:
            calls = 0
            async def screenshot(self):
                self.calls += 1
                if self.calls == 8:
                    (owner.run.path / 'STOP').write_text('user stop')
                return None, {'signal': False}

            async def keys(self, keys):
                owner.keys.append((time.monotonic(), keys))
        result = self.execute(Client())
        self.assertEqual(result['status'], 'stopped')
        self.assertGreaterEqual(sum(e['event'] == 'no_video' for e in self.events()), 8)
        self.assertFalse(any(e['event'] in ('awaiting_bios_anchor', 'missed_bios_entry') for e in self.events()))

    def test_ocr_failure_and_unknown_visible_output_pause_instead_of_infinite_hid(self):
        for answer in ({'ok': False, 'classification': 'unknown', 'error': 'OCR unavailable'},
                       {'ok': True, 'classification': 'unknown', 'text': 'unclassified visible screen'}):
            with self.subTest(answer=answer):
                # Reset only the fake ledger stage; no target action exists in this fixture.
                self.run.update(stage='created')
                client = self.client([b'unknown'])
                with patch('nanokvm_flow.workflow.read_text', return_value=answer) as ocr:
                    result = asyncio.run(wait_video(client, self.run, poll_s=.001, send_delete=True))
                self.assertEqual(result['status'], 'entry_unclassified')
                self.assertEqual(ocr.call_count,3 if answer['ok'] is False else 1)

    def test_punctuation_cursor_keeps_readonly_observation_and_stop_effective(self):
        owner=self
        class Client:
            calls=0
            async def screenshot(self):
                self.calls+=1
                await asyncio.sleep(.04)
                if self.calls==10:(owner.run.path/'STOP').write_text('stop')
                return b'_',{}
            async def keys(self,keys):owner.keys.append((time.monotonic(),keys))
        result=self.execute(Client())
        self.assertEqual(result['status'],'stopped')
        self.assertLessEqual(len(self.keys),1)

    def test_unknown_cursor_keeps_windows_probe_effective_after_hid_stops(self):
        async def probe():
            await asyncio.sleep(.06)
            return {'boot_id':'new','explorer_pid':2,'session_id':1}
        result=self.execute(self.client([b'_'],delay=.01),windows_probe=probe)
        self.assertEqual(result['status'],'missed_bios_entry')
        self.assertLessEqual(len(self.keys),1)

    def test_new_boot_explorer_can_stop_del_while_frame_call_is_pending(self):
        async def probe():
            await asyncio.sleep(.02)
            return {'source': 'fake_explorer', 'boot_id': 'new-boot', 'explorer_pid': 1, 'session_id': 1}
        result = self.execute(self.client([b'AORUS'], delay=.2), windows_probe=probe)
        self.assertEqual(result['status'], 'missed_bios_entry')
        self.assertEqual(result['windows_evidence']['boot_id'], 'new-boot')
        self.assertLessEqual(len(self.keys), 1)

    def test_known_user_welcome_is_generalized_and_bare_logo_never_bios(self):
        self.assertEqual(_entry_classification({'ok': True, 'text': 'ALICE Welcome'}, 'Alice'), 'windows')
        self.assertEqual(_entry_classification({'ok': True, 'text': 'AORUS'}), 'logo')
        self.assertEqual(_entry_classification({'ok': True, 'text': 'Welcome'}), 'unknown')


if __name__ == '__main__':
    unittest.main()
