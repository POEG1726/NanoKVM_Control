import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from nanokvm_flow.vision import classify_text, read_text, tweaker_root_match


class VisionTests(unittest.TestCase):
    def test_calibrated_root_requires_all_independent_fields(self):
        text = 'Tweaker ADVANCED MODE CPU Clock Control System Memory Multiplier UCLK=MEMCLK Advanced Memory Settings'
        self.assertTrue(tweaker_root_match({'ok':True,'text':text})['matched'])
        for field in ('CPU Clock Control','System Memory Multiplier','UCLK=MEMCLK','Advanced Memory Settings'):
            self.assertFalse(tweaker_root_match({'ok':True,'text':text.replace(field,'')})['matched'])
        for bad in (text+' Are you sure?', 'Tweaker Advanced Mode DDR Bus Configuration',
                    'Tweaker Advanced Mode CPU Clock Control', 'AORUS'):
            self.assertFalse(tweaker_root_match({'ok':True,'text':bad})['matched'])
        self.assertFalse(tweaker_root_match({'ok':False,'text':text})['matched'])

    def test_explicit_failure_titles(self):
        for text in ('Boot failure detected', 'BOOT\nFAILURE DETECTED',
                     'The system has experienced a boot failure because of overclocking.'):
            self.assertEqual(classify_text(text), 'failure')

    def test_black_or_unknown_is_not_failure(self):
        for text in ('', '  ', None, 'No HDMI signal detected', 'Training memory...',
                     'TM5 Test failed', 'Stress test failed with 1 error.',
                     'Windows Boot Manager', 'Load Profiles', 'POST'):
            self.assertEqual(classify_text(text), 'unknown')

    def test_normal_bios(self):
        self.assertEqual(classify_text('AORUS ADVANCED MODE Tweaker Save & Exit'), 'BIOS')
        self.assertEqual(classify_text('VAL7RZJ5 Tweaker ADVANCED MODE Save & Exit'), 'BIOS')

    def test_windows(self):
        self.assertEqual(classify_text('Windows sign-in options'), 'windows')

    def test_missing_image_unknown(self):
        self.assertEqual(read_text('definitely-missing.jpg')['classification'], 'unknown')

    def test_success_uses_stdout_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'frame.jpg'; p.write_bytes(b'image')
            completed = subprocess.CompletedProcess([], 0, json.dumps({'ok': True, 'text': 'Boot failure detected'}), '')
            with patch('nanokvm_flow.vision.subprocess.run', return_value=completed):
                self.assertEqual(read_text(p, executable='test-powershell')['classification'], 'failure')

    def test_ocr_error_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'frame.jpg'; p.write_bytes(b'image')
            for completed in (subprocess.CompletedProcess([], 1, json.dumps({'ok': False, 'error': 'no language'}), ''),
                              subprocess.CompletedProcess([], 0, 'not JSON', '')):
                with patch('nanokvm_flow.vision.subprocess.run', return_value=completed):
                    result = read_text(p, executable='test-powershell')
                    self.assertFalse(result['ok'])
                    self.assertEqual(result['classification'], 'unknown')

    def test_timeout_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'frame.jpg'; p.write_bytes(b'image')
            with patch('nanokvm_flow.vision.subprocess.run', side_effect=subprocess.TimeoutExpired('ocr', 1)):
                self.assertEqual(read_text(p, executable='test-powershell')['classification'], 'unknown')


if __name__ == '__main__':
    unittest.main()
