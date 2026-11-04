"""Export an allowlisted source snapshot without runtime data or Git history.

Offline only. Never overwrites a destination; never executes copied code.
Review the resulting manifest and source before publishing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


PUBLIC_FILES = (
    'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', '.gitignore', '.gitattributes',
    'requirements.txt', 'run.ps1', '.github/workflows/offline-tests.yml',
    'config/host.example.json', 'config/dsview.example.json',
    'target/README.md', 'target/config.example.json',
    'docs/setup.md', 'docs/workflow.md', 'docs/dsview.md', 'docs/queue.md',
    'docs/bios-calibration.md', 'docs/agent-workflow.md', 'docs/publishing.md',
    'profiles/b850m_f12d.json', 'profiles/b850m_f12d_vddp_5mv.json',
)
CODE_PATTERNS = {
    'nanokvm_flow': ('*.py',), 'target': ('*.py', '*.ps1'),
    'scripts': ('*.ps1',), 'tools': ('*.py',), 'tests': ('test_*.py',),
}
TOKEN = re.compile(
    rb'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----|'
    rb'gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}'
)


def public_paths(source):
    paths = set(PUBLIC_FILES)
    for folder, patterns in CODE_PATTERNS.items():
        for pattern in patterns:
            paths.update(p.relative_to(source).as_posix() for p in (source / folder).glob(pattern))
    return sorted(paths)


def publication_bytes(source, relative):
    path = source / relative
    # Refuse links at every component, not just the leaf: never follow a link
    # into credentials, a mounted data folder, or another checkout.
    for part in (path, *path.parents):
        if part == source.parent:
            break
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('Linked publication input: ' + relative)
    content = path.read_bytes()
    if relative.startswith('profiles/'):
        profile = json.loads(content.decode('utf-8-sig'))
        profile['evidence']['static_vddp'] = (
            'F12d static mapping: Tweaker Setup+0x14C u16, 5mV step; '
            'historical analysis is external to this source distribution.'
        )
        verification = profile.get('readback_verification', {})
        if verification.get('saved_request_reader'):
            verification['saved_request_reader'] = 'external/bios-readback/verify_vddp_request.py'
        content = (json.dumps(profile, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    # Keep manifest hashes stable across Git checkout on Windows and Linux.
    text = content.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n').rstrip('\n') + '\n'
    if relative.endswith('.ps1'):
        text = text.replace('\n', '\r\n')
    content = text.encode('utf-8')
    if TOKEN.search(content):
        raise ValueError('Potential credential in publication input: ' + relative)
    return content


def prepare(source, output):
    source = Path(source).absolute()
    output = Path(output).absolute()
    resolved_source, resolved_output = source.resolve(), output.resolve()
    if resolved_output == resolved_source or resolved_source.is_relative_to(resolved_output):
        raise ValueError('Publication output must not contain or replace the source tree')
    if output.exists() or output.is_symlink():
        raise FileExistsError('Publication destination already exists')
    # Validate/read all allowed inputs before creating output. No recursive
    # enumeration of .git, .local, run data, home, or authentication providers.
    files = [(relative, publication_bytes(source, relative)) for relative in public_paths(source)]
    output.mkdir(parents=True, exist_ok=False)
    manifest = {'schema_version': 1, 'history_included': False, 'files': []}
    for relative, content in files:
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(content)
        manifest['files'].append({'name': relative, 'size': len(content),
                                  'sha256': hashlib.sha256(content).hexdigest()})
    (output / 'publication-manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = prepare(Path(__file__).resolve().parents[1], args.output)
    print(json.dumps({'output': str(args.output.resolve()), 'files': len(manifest['files']),
                      'history_included': False}))


if __name__ == '__main__':
    main()
