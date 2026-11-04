import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from nanokvm_flow.boot_entry import enter_bios
from nanokvm_flow.state import Run


class BootEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.run = Run(self.tmp.name, 'entry'); self.run.create({}, 0)
        self.order = []; self.images = [b'windows', None, None, b'new-bios-image']
        self.ocr = lambda path: {'ok': True, 'classification': 'BIOS', 'text': 'Tweaker Advanced Mode'}
        owner = self
        class Client:
            def __init__(self, config): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def screenshot(self):
                image = owner.images.pop(0)
                owner.order.append('picture' if image else 'no_signal')
                await asyncio.sleep(0)
                return image, {}
            async def keys(self, keys): owner.order.append(('keys', keys))
            async def text(self, text): owner.order.append(('text', text))
        self.client = Client
        def ps(command):
            if 'shutdown.exe' in command:
                self.order.append('restart'); return '{"requested":true,"exit_code":0}'
            self.order.append('stress_check'); return '{"stress":[]}'
        self.target = Mock(); self.target.ps.side_effect = ps

    def execute(self, **kwargs):
        with patch('nanokvm_flow.boot_entry.ensure_capture_started', side_effect=lambda *args: self.order.append('capture')), \
             patch('nanokvm_flow.boot_entry.Notifier') as notifier, \
             patch('nanokvm_flow.workflow.read_text', side_effect=self.ocr):
            notifier.return_value.notify.return_value = {'status': 'queued'}
            result = asyncio.run(enter_bios({'notifications': {}, **getattr(self,'config_overrides',{})}, self.run,
                                         target=self.target, client_factory=self.client, poll_s=0, **kwargs))
            self.notifications = notifier.return_value.notify.call_args_list
            return result

    def test_capture_restart_loss_then_delete_and_new_anchor_queue(self):
        result = self.execute()
        self.assertEqual(self.order[:3], ['stress_check', 'capture', 'restart'])
        first_key = next(index for index, item in enumerate(self.order) if isinstance(item, tuple))
        self.assertGreater(first_key, self.order.index('no_signal'))
        self.assertTrue(all(item[1] == ['Delete'] for item in self.order if isinstance(item, tuple)))
        self.assertEqual(result['status'], 'awaiting_bios_anchor')
        self.assertEqual(self.run.load()['stage'], 'awaiting_bios_anchor')
        self.assertTrue((self.run.path / result['image']['path']).is_file())
        self.assertEqual(self.order.count('restart'), 1)
        self.assertEqual(self.notifications, [])

    def _auto_profile(self):
        profile = json.loads((Path(__file__).resolve().parents[1]/'profiles/b850m_f12d_vddp_5mv.json').read_text(encoding='utf-8'))
        self.run.update(profile=profile,slot=25)
        return profile

    def test_transient_cursor_to_root_applies_and_notifies_only_f10(self):
        from nanokvm_flow.bios import build_actions
        profile = self._auto_profile()
        root = b'Tweaker ADVANCED MODE CPU Clock Control System Memory Multiplier UCLK=MEMCLK Advanced Memory Settings'
        self.images = [b'old-windows',None,b'_',root,b'1025',b'Profile Saved',b'Profile8 agent-tune',b'F10 changes']
        self.ocr = lambda path: {'ok':True,'text':Path(path).read_bytes().decode(),
                                'classification':'BIOS' if Path(path).read_bytes()==root else 'unknown'}
        actions=build_actions(profile,25)
        for action in actions:
            if action['op']=='wait':action['ms']=0
            if action['op']=='key':action['interval_ms']=0
        with patch('nanokvm_flow.boot_entry.build_actions',return_value=actions):
            result = self.execute(firmware_ui=True,auto_apply=True)
        self.assertEqual(result['status'],'awaiting_f10_review')
        self.assertEqual(self.run.load()['stage'],'awaiting_f10_review')
        self.assertEqual([call.args[1] for call in self.notifications],['f10_review'])
        keys=[item[1] for item in self.order if isinstance(item,tuple) and item[0]=='keys']
        self.assertEqual(keys[-1],['F10'])
        self.assertNotIn(['Delete'],keys)
        self.assertTrue(self.run.load()['anchor_image']['root_match']['matched'])
        self.assertTrue((self.run.path/'review_request.json').is_file())

    def test_auto_apply_rejects_bios_submenu_without_root_fields(self):
        self._auto_profile()
        self.ocr=lambda path:{'ok':True,'classification':'BIOS','text':'Tweaker Advanced Mode DDR Bus Configuration'}
        result=self.execute(firmware_ui=True,auto_apply=True)
        self.assertEqual(result['status'],'entry_unclassified')
        self.assertFalse(any(isinstance(item,tuple) for item in self.order))
        self.assertEqual([call.args[1] for call in self.notifications],['bios_entry_unclassified'])

    def test_auto_apply_rejects_unconfigured_profile_before_reboot(self):
        with self.assertRaises(RuntimeError):self.execute(auto_apply=True)
        self.target.ps.assert_not_called()

    def test_config_enables_auto_apply_without_cli_flag(self):
        self.config_overrides={'bios_entry_auto_apply':True}
        with self.assertRaisesRegex(RuntimeError,'Auto-apply requires'):self.execute()
        self.target.ps.assert_not_called()

    def test_explicit_firmware_ui_is_single_dispatch_and_receipted(self):
        result = self.execute(firmware_ui=True)
        self.assertEqual(result['status'], 'awaiting_bios_anchor')
        receipt = json.loads((self.run.path / 'restart-request.json').read_text())
        self.assertEqual(receipt['command'], 'shutdown /r /fw /t 0')
        self.assertIs(receipt['firmware_ui'], True)
        commands = [call.args[0] for call in self.target.ps.call_args_list
                    if 'shutdown.exe' in call.args[0]]
        self.assertEqual(len(commands), 1)
        self.assertIn('shutdown.exe /r /fw /t 0;', commands[0])
        again = self.execute()
        self.assertEqual(again['status'], 'restart_already_attempted')
        self.assertEqual(self.order.count('restart'), 1)

    def test_unapproved_stage_cannot_reboot_or_capture(self):
        self.run.update(stage='awaiting_f10_review')
        with self.assertRaises(RuntimeError): self.execute()
        self.assertEqual(self.order, [])
        self.target.ps.assert_not_called()

    def test_running_stress_prevents_capture_restart_and_hid(self):
        self.target.ps.side_effect = lambda command: '{"stress":[{"Name":"y-cruncher.exe"}]}'
        with self.assertRaises(RuntimeError): self.execute()
        self.assertEqual(self.order, [])
        self.assertFalse((self.run.path / 'restart-request.json').exists())

    def test_capture_failure_does_not_gate_restart(self):
        with patch('nanokvm_flow.boot_entry.ensure_capture_started', side_effect=RuntimeError('capture failed')), \
             patch('nanokvm_flow.boot_entry.Notifier') as notifier, \
             patch('nanokvm_flow.workflow.read_text', return_value={'ok': True, 'classification': 'BIOS', 'text': 'Tweaker Advanced Mode'}):
            notifier.return_value.notify.return_value = {'status': 'queued'}
            result = asyncio.run(enter_bios({'notifications': {}}, self.run, target=self.target,
                                           client_factory=self.client, poll_s=0))
        self.assertEqual(result['status'], 'awaiting_bios_anchor')
        self.assertEqual(self.order.count('restart'), 1)

    def test_restart_ssh_disconnect_is_uncertain_and_never_retried(self):
        original = self.target.ps.side_effect
        def ps(command):
            if 'shutdown.exe' in command:
                self.order.append('restart'); raise ConnectionError('SSH disconnected')
            return original(command)
        self.target.ps.side_effect = ps
        self.execute()
        receipt = json.loads((self.run.path / 'restart-request.json').read_text())
        self.assertEqual(receipt['status'], 'unconfirmed_transport')
        self.assertEqual(self.order.count('restart'), 1)

    def test_stop_during_windows_screen_does_not_send_delete(self):
        owner = self
        class StopClient:
            def __init__(self, config): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def screenshot(self):
                (owner.run.path / 'STOP').write_text('stop')
                return b'windows', {}
            async def keys(self, keys): owner.fail('Unexpected HID')
        self.client = StopClient
        result = self.execute()
        self.assertEqual(result['status'], 'stopped')
        self.assertFalse(any(isinstance(item, tuple) for item in self.order))

    def test_stop_during_delete_loop_stops_without_restarting_again(self):
        owner = self
        class StopDeleteClient:
            def __init__(self, config): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def screenshot(self):
                await asyncio.sleep(0)
                return None, {}
            async def keys(self, keys):
                owner.order.append(('keys', keys))
                (owner.run.path / 'STOP').write_text('stop')
        self.client = StopDeleteClient
        result = self.execute()
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(self.order.count('restart'), 1)
        self.assertEqual(sum(isinstance(item, tuple) for item in self.order), 1)

    def test_windows_restart_rejection_does_not_send_delete(self):
        original = self.target.ps.side_effect
        def ps(command):
            if 'shutdown.exe' in command:
                self.order.append('restart'); return '{"requested":false,"exit_code":5}'
            return original(command)
        self.target.ps.side_effect = ps
        with self.assertRaisesRegex(RuntimeError, 'Windows rejected restart'):
            self.execute()
        receipt = json.loads((self.run.path / 'restart-request.json').read_text())
        self.assertEqual(receipt['status'], 'rejected_not_initiated')
        self.assertFalse(any(isinstance(item, tuple) for item in self.order))

    def test_new_boot_explorer_fallback_marks_missed_without_retry(self):
        original = self.target.ps.side_effect
        def ps(command):
            if 'Get-Process explorer' in command:
                return '{"explorer_pid":100,"session_id":1,"boot_id":"2999-01-01T00:00:00Z"}'
            return original(command)
        self.target.ps.side_effect = ps
        self.images = [b'windows', None] + [b'AORUS'] * 100
        self.ocr = lambda path: {'ok': True, 'classification': 'unknown', 'text': 'AORUS'}
        result = self.execute()
        self.assertEqual(result['status'], 'missed_bios_entry')
        self.assertEqual(self.order.count('restart'), 1)
        self.assertEqual(self.run.load()['stage'], 'missed_bios_entry')
        self.assertEqual(result['windows_evidence']['explorer_pid'], 100)

    def test_old_boot_explorer_is_not_evidence_of_missing_this_entry(self):
        original = self.target.ps.side_effect
        def ps(command):
            if 'Get-Process explorer' in command:
                return '{"explorer_pid":100,"session_id":1,"boot_id":"2000-01-01T00:00:00Z"}'
            return original(command)
        self.target.ps.side_effect = ps
        result = self.execute()
        self.assertEqual(result['status'], 'awaiting_bios_anchor')
        self.assertFalse('windows_evidence' in result)


if __name__ == '__main__': unittest.main()
