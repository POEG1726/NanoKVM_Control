"""Offline tests: saved BIOS requests distinguish neighboring 5mV points."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nanokvm_flow.host import Target, verify_vddp
from nanokvm_flow.state import Run, sha
from nanokvm_flow.worker import monitor
from nanokvm_flow.__main__ import main


class ExactReadbackTests(unittest.TestCase):
    def fixture(self, root):
        root = Path(root)
        reader = root / 'reader.py'; reader.write_text('# offline parser fixture\n')
        dump = root / 'dump.bin'; dump.write_bytes(b'offline dump')
        zen = root / 'zen.json'
        zen.write_text(json.dumps({'readings': {'power_table': {'cldo_vddp_v': 1.0225}}}))
        manifest = {'files': [{'name': p.name, 'size': p.stat().st_size, 'sha256': sha(p)}
                              for p in (dump, zen)], 'hwtrace': {'pid': 12}}
        profile = {'readback_verification': {'require_exact_saved_requests': True,
                                            'saved_request_reader': str(reader)}}
        result = {'ok': True, 'exact_match': True, 'manual_match': True, 'expected_mv': 1025,
                  'dump': {'path': str(dump), 'size': dump.stat().st_size,
                           'sha256': sha(dump), 'unchanged_after_verification': True},
                  'fields': {'tweaker_vddp': {'raw': 1025}, 'aod_vddp': {'raw': 1025},
                             'aod_control': {'raw': 1}}}
        return manifest, profile, result

    def completed(self, result, code=0):
        return SimpleNamespace(returncode=code, stdout=json.dumps(result), stderr='')

    def test_exact_then_smu_and_explicit_parser_argv(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, profile, result = self.fixture(temp)
            with patch('nanokvm_flow.host.subprocess.run', return_value=self.completed(result)) as execute:
                readback = verify_vddp(manifest, temp, 1025, profile=profile)
            self.assertEqual(readback['reported_v'], 1.0225)
            self.assertEqual(readback['saved_request_verification']['result'], result)
            argv = execute.call_args.args[0]
            self.assertEqual(argv, [sys.executable, str(Path(temp)/'reader.py'), '--dump',
                                    str((Path(temp)/'dump.bin').resolve()), '--expected-mv', '1025'])
            self.assertNotIn('shell', execute.call_args.kwargs)

    def test_neighbor_blocked_despite_smu_inside_twenty_mv(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, profile, result = self.fixture(temp)
            result.update(ok=False, exact_match=False)
            result['fields']['tweaker_vddp']['raw'] = 1020
            with patch('nanokvm_flow.host.subprocess.run', return_value=self.completed(result, 1)):
                with self.assertRaisesRegex(RuntimeError, 'saved requests'):
                    verify_vddp(manifest, temp, 1025, profile=profile)

    def test_invalid_parser_results_and_binding_rejected(self):
        mutations = [lambda r: r.update(manual_match=False),
                     lambda r: r.update(expected_mv=1020),
                     lambda r: r['dump'].update(sha256='0'*64),
                     lambda r: r['dump'].update(unchanged_after_verification=False)]
        for mutate in mutations:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as temp:
                manifest, profile, result = self.fixture(temp); mutate(result)
                with patch('nanokvm_flow.host.subprocess.run', return_value=self.completed(result)):
                    with self.assertRaises(RuntimeError):
                        verify_vddp(manifest, temp, 1025, profile=profile)

    def test_parser_invalid_json_and_timeout_terminal(self):
        import subprocess
        for response in (SimpleNamespace(returncode=0, stdout='not JSON'),
                         subprocess.TimeoutExpired(['reader'], 30)):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as temp:
                manifest, profile, _ = self.fixture(temp)
                kwargs = {'side_effect': response} if isinstance(response, Exception) else {'return_value': response}
                with patch('nanokvm_flow.host.subprocess.run', **kwargs):
                    with self.assertRaises(RuntimeError):
                        verify_vddp(manifest, temp, 1025, profile=profile)

    def test_legacy_profile_never_invokes_parser(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, _, _ = self.fixture(temp)
            with patch('nanokvm_flow.host.subprocess.run') as execute:
                for profile in (None, {}, {'readback_verification': {'require_exact_saved_requests': False}}):
                    self.assertEqual(verify_vddp(manifest, temp, 1025, profile=profile)['reported_v'], 1.0225)
            execute.assert_not_called()

    def test_receipt_retains_full_exact_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, profile, result = self.fixture(temp)
            run = Run(temp, 'receipt'); run.create(profile, 0)
            file = run.path/'manifest.json'; file.write_text(json.dumps(manifest))
            with patch('nanokvm_flow.host.subprocess.run', return_value=self.completed(result)):
                readback = verify_vddp(manifest, temp, 1025, profile=profile)
            target = Target({'target_root': r'D:\OCBootCapture'})
            with patch.object(target, 'write_json') as upload:
                target.receipt(run, manifest, file, {'session': 'fake', 'seq': 1, 'timestamp': '2026-11-04T00:00:00Z'}, readback=readback)
            local = json.loads((run.path/'received.json').read_text())
            self.assertEqual(local['readback'], readback)
            self.assertEqual(upload.call_args.args[1], local)

    def setup_run(self, temp):
        manifest, profile, result = self.fixture(temp)
        run = Run(temp, 'neighbor'); run.create(profile, 0); run.update(stage='waiting_windows')
        file = Path(temp)/'manifest.json'; file.write_text(json.dumps(manifest))
        # Skip unrelated desktop discovery; no target connection is performed.
        (run.path/'desktop-ready.json').write_text('{}')
        result.update(ok=False, exact_match=False)
        return run, manifest, file, result

    def test_worker_neighbor_never_receipts_or_launches_test(self):
        with tempfile.TemporaryDirectory() as temp:
            run, manifest, file, result = self.setup_run(temp)
            with patch('nanokvm_flow.worker.Target') as target, patch('nanokvm_flow.worker.Notifier') as notify, \
                    patch('nanokvm_flow.worker.record_desktop_ready'), \
                    patch('nanokvm_flow.worker.expected_changes', return_value=[{'to_mv': 1025}]), \
                    patch('nanokvm_flow.host.subprocess.run', return_value=self.completed(result, 1)), \
                    patch('nanokvm_flow.worker.telemetry_current') as telemetry:
                target.return_value.fetch_ready.return_value = (manifest, file)
                notify.return_value.notify.return_value = {'status': 'queued'}
                asyncio.run(monitor({'notifications': {}}, run))
                target.return_value.receipt.assert_not_called()
                target.return_value.command.assert_not_called()
                telemetry.assert_not_called()
            self.assertEqual(run.load()['stage'], 'needs_attention')

    def test_manual_postboot_neighbor_never_receipts_or_launches_test(self):
        with tempfile.TemporaryDirectory() as temp:
            run, manifest, file, result = self.setup_run(temp)
            config = Path(temp)/'config.json'; config.write_text(json.dumps({'runs_root': temp}))
            with patch.object(sys, 'argv', ['nanokvm_flow', '--config', str(config), 'postboot', '--run', run.name]), \
                    patch('nanokvm_flow.__main__.Target') as target, \
                    patch('nanokvm_flow.hdmi_watch.start_background'), \
                    patch('nanokvm_flow.worker.record_desktop_ready', return_value=True), \
                    patch('nanokvm_flow.__main__.expected_changes', return_value=[{'to_mv': 1025}]), \
                    patch('nanokvm_flow.host.subprocess.run', return_value=self.completed(result, 1)):
                target.return_value.fetch_ready.return_value = (manifest, file)
                with self.assertRaisesRegex(RuntimeError, 'saved requests'): main()
                target.return_value.receipt.assert_not_called()
                target.return_value.command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
