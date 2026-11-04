import json
from pathlib import Path, PureWindowsPath
import tempfile
import unittest
from unittest.mock import Mock

from nanokvm_flow.host import Target
from nanokvm_flow.state import sha


class HeadlessDeployTests(unittest.TestCase):
    def bundle(self, directory):
        files=[]
        for name in ('ZenTimings.Export.exe','ZenStates-Core.dll','inpoutx64.dll','ZenTimings.Export.exe.config'):
            path=directory/name
            path.write_bytes(('test-'+name).encode())
            files.append({'name':name,'size':path.stat().st_size,'sha256':sha(path)})
        (directory/'build-manifest.json').write_text(json.dumps({'files':files}))

    def test_uploads_complete_bundle_in_separate_version_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);self.bundle(folder)
            target=Target({'target_root':r'D:\OCBootCapture','zentimings_cli_bundle':temp})
            target.ps=Mock();target.copy=Mock()
            executable=PureWindowsPath(target.deploy_zentimings_cli())
            self.assertEqual(executable.name,'ZenTimings.Export.exe')
            self.assertEqual(executable.parent.parent.name,'ZenTimingsCLI')
            self.assertEqual(target.copy.call_count,5)
            self.assertIn('Get-FileHash',target.ps.call_args.args[0])

    def test_corrupt_bundle_is_rejected_before_any_remote_operation(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);self.bundle(folder)
            (folder/'ZenStates-Core.dll').write_bytes(b'changed')
            target=Target({'target_root':r'D:\OCBootCapture','zentimings_cli_bundle':temp})
            target.ps=Mock();target.copy=Mock()
            with self.assertRaisesRegex(RuntimeError,'hash mismatch'):
                target.deploy_zentimings_cli()
            target.ps.assert_not_called();target.copy.assert_not_called()

    def test_existing_configuration_keeps_user_paths_and_receives_cli(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)
            (source/'pipeline.py').write_text('# test')
            previous={'hwinfo':'custom-hwi.exe','zentimings':'old-gui.exe','tests':{'vt3':{'repeats':3,'expected_count':3,'arguments':['custom-workload']}}}
            target=Target({'target_root':r'D:\OCBootCapture'})
            def ps(code):
                if code.startswith('Test-Path'):return 'True'
                if code.startswith('Get-Content'):return json.dumps(previous)
                return ''
            target.ps=Mock(side_effect=ps);target.copy=Mock();target.write_json=Mock();target.arm=Mock()
            target.deploy_zentimings_cli=Mock(return_value=r'D:\OCBootCapture\ZenTimingsCLI\version\ZenTimings.Export.exe')
            target.deploy(source,Mock(name='R1'),start_capture=False)
            actual=target.write_json.call_args.args[1]
            self.assertNotIn('zentimings',actual)
            self.assertEqual(actual['hwinfo'],'custom-hwi.exe')
            self.assertEqual(actual['tests']['vt3']['repeats'],10)
            self.assertEqual(actual['tests']['vt3']['arguments'],['custom-workload'])
            self.assertTrue(actual['zentimings_cli']['executable'].endswith('ZenTimings.Export.exe'))
            self.assertFalse(any('Start-ScheduledTask' in call.args[0] for call in target.ps.call_args_list))


if __name__=='__main__':unittest.main()
