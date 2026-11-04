"""Offline publication boundary tests; no credentials or hardware are read."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import prepare_github as publication


class PublicationTests(unittest.TestCase):
    def test_export_manifest_excludes_runtime_and_preserves_profile_source(self):
        source = Path(__file__).resolve().parents[1]
        profile = source / 'profiles/b850m_f12d_vddp_5mv.json'
        original = profile.read_bytes()
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'public'
            manifest = publication.prepare(source, output)
            names = {row['name'] for row in manifest['files']}
            for forbidden in ('AGENTS.md', 'handoff.md', 'config/host.json',
                              'config/dsview.json', 'docs/archive.md'):
                self.assertNotIn(forbidden, names)
            self.assertFalse(any(name.startswith(('runs/', '.git/', 'config/scans/')) for name in names))
            self.assertEqual(names, {p.relative_to(output).as_posix() for p in output.rglob('*')
                                     if p.is_file() and p.name != 'publication-manifest.json'})
            for row in manifest['files']:
                data = (output / row['name']).read_bytes()
                self.assertEqual(len(data), row['size'])
                self.assertEqual(hashlib.sha256(data).hexdigest(), row['sha256'])
            public_profile = json.loads((output / profile.relative_to(source)).read_text())
            self.assertTrue(public_profile['readback_verification']['require_exact_saved_requests'])
            self.assertEqual(public_profile['readback_verification']['saved_request_reader'],
                             'external/bios-readback/verify_vddp_request.py')
            self.assertEqual(profile.read_bytes(), original)
            with self.assertRaises(FileExistsError):
                publication.prepare(source, output)

    def test_secret_candidate_refused_before_creating_output(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source'; source.mkdir()
            (source / 'README.md').write_bytes(b'-----BEGIN ' + b'PRIVATE KEY-----')
            output = Path(folder) / 'public'
            with patch.object(publication, 'PUBLIC_FILES', ('README.md',)), \
                    patch.object(publication, 'CODE_PATTERNS', {}):
                with self.assertRaisesRegex(ValueError, 'credential'):
                    publication.prepare(source, output)
            self.assertFalse(output.exists())

    def test_linked_input_is_not_followed(self):
        # is_symlink is mocked so this test also works without Windows symlink privilege.
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source'; source.mkdir()
            output = Path(folder) / 'public'
            with patch.object(publication, 'PUBLIC_FILES', ('README.md',)), \
                    patch.object(publication, 'CODE_PATTERNS', {}), \
                    patch.object(Path, 'is_symlink', lambda path: path == source / 'README.md'):
                with self.assertRaisesRegex(ValueError, 'Linked'):
                    publication.prepare(source, output)
            self.assertFalse(output.exists())

    def test_source_and_ancestor_cannot_be_destinations(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source'; source.mkdir()
            for output in (source, source.parent):
                with self.assertRaises(ValueError):
                    publication.prepare(source, output)


if __name__ == '__main__':
    unittest.main()
