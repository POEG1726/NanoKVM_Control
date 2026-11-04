"""Offline JSON transport checks; no SSH or target operations."""
import base64
import json
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from nanokvm_flow.host import Target


class JsonTransferTests(unittest.TestCase):
    def target(self):
        return Target({'target_root': r'D:\OCBootCapture'})

    def test_large_json_is_scp_data_and_short_hash_bound_command(self):
        target = self.target()
        obj = {'readback': 'evidence'*50000, 'unicode': '原始请求'}
        copied = []
        def copy(source, destination, upload=False):
            self.assertTrue(upload)
            self.assertTrue(Path(source).is_file())
            copied.append((Path(source), destination, Path(source).read_bytes()))
        target.copy = Mock(side_effect=copy)
        target.ps = Mock(return_value='')
        target.write_json(r'D:\OCBootCapture\received.json', obj)
        source, remote, data = copied[0]
        self.assertEqual(json.loads(data), obj)
        self.assertEqual(remote.parent, PureWindowsPath(r'D:\OCBootCapture'))
        self.assertTrue(remote.name.startswith('.received.json.'))
        self.assertTrue(remote.name.endswith('.tmp'))
        self.assertFalse(source.exists())
        command = target.ps.call_args.args[0]
        self.assertNotIn('FromBase64String', command)
        self.assertIn('Get-FileHash', command)
        self.assertIn('[IO.File]::Move', command)
        self.assertNotIn('evidence', command)
        prefix = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
        self.assertLess(len(base64.b64encode((prefix+command).encode('utf-16-le'))), 4000)

    def test_failed_upload_never_attempts_final_replace(self):
        target = self.target()
        target.copy = Mock(side_effect=RuntimeError('offline SCP failure'))
        target.ps = Mock()
        with self.assertRaises(RuntimeError):
            target.write_json(r'D:\OCBootCapture\received.json', {'ok': True})
        target.ps.assert_not_called()

    def test_retries_use_distinct_remote_temporary_names(self):
        target = self.target(); target.copy = Mock(); target.ps = Mock()
        target.write_json(r'D:\OCBootCapture\received.json', {})
        target.write_json(r'D:\OCBootCapture\received.json', {})
        names = [call.args[1] for call in target.copy.call_args_list]
        self.assertNotEqual(names[0], names[1])

    @unittest.skipUnless(shutil.which('pwsh'), 'local PowerShell required for offline syntax check')
    def test_actual_powershell_validates_and_replaces_only_verified_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            final = Path(directory)/'received.json'; final.write_text('{"old":true}')
            target = self.target()
            target.ps = lambda code: subprocess.run(
                ['pwsh', '-NoProfile', '-Command', "$ErrorActionPreference='Stop'; "+code],
                check=True, capture_output=True, text=True).stdout
            def copied(source, destination, upload=False):
                shutil.copyfile(source, str(destination))
            target.copy = copied
            target.write_json(final, {'new': True, 'unicode': '值'})
            self.assertEqual(json.loads(final.read_text(encoding='utf-8')), {'new': True, 'unicode': '值'})
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])
            def corrupted(source, destination, upload=False):
                Path(str(destination)).write_bytes(b'corrupt')
            target.copy = corrupted
            with self.assertRaises(subprocess.CalledProcessError):
                target.write_json(final, {'replacement': True})
            self.assertEqual(json.loads(final.read_text(encoding='utf-8')), {'new': True, 'unicode': '值'})


if __name__ == '__main__':
    unittest.main()
