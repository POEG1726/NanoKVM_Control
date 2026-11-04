"""Small durable run ledger. F10 approvals bind the exact plan and image set."""
import datetime as dt
import hashlib
import json
import re
from pathlib import Path


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def object_sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class Run:
    def __init__(self, root, name):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
            raise ValueError('Invalid run id')
        self.name = name
        self.path = Path(root) / name
        self.file = self.path / 'state.json'

    def create(self, profile, slot):
        self.path.mkdir(parents=True, exist_ok=False)
        self.save({'run_id': self.name, 'stage': 'created', 'profile': profile,
                   'slot': slot, 'created_utc': utc(), 'actions': [], 'evidence': []})

    def load(self):
        return json.loads(self.file.read_text(encoding='utf-8'))

    def save(self, state):
        tmp = self.file.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding='utf-8')
        tmp.replace(self.file)

    def update(self, **changes):
        s = self.load(); s.update(changes); self.save(s)
        return s

    def event(self, event, **details):
        with (self.path / 'events.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps({'utc': utc(), 'event': event, **details}, ensure_ascii=False) + '\n')

    def image(self, data, label):
        s = self.load()
        p = self.path / f'{len(s["evidence"]):03d}_{label}.jpg'
        with p.open('xb') as f:
            f.write(data)
        item = {'path': p.name, 'sha256': sha(p), 'utc': utc(), 'label': label}
        s['evidence'].append(item); self.save(s)
        return item

    def review_request(self, expected):
        s = self.load()
        evidence = [e for e in s['evidence'] if e['label'].startswith(('f10','profile8_saved'))]
        if not any(e['label'].startswith('f10') for e in evidence):
            raise RuntimeError('Missing F10 image')
        request = {'run_id': self.name, 'expected_changes': expected,
                   'profile_sha256': object_sha(s['profile']), 'slot': s['slot'],
                   'expected_profile_storage': s['profile'].get('profile_storage') if s['profile'].get('save_before_f10') else None,
                   'evidence': evidence, 'created_utc': utc(),
                   'instruction': 'Inspect the entire F10 change list, including any scrolled pages; accept only the exact expected changes.'}
        request['request_sha256'] = object_sha(request)
        (self.path / 'review_request.json').write_text(json.dumps(request, indent=2, ensure_ascii=False), encoding='utf-8')
        self.update(stage='awaiting_f10_review')
        return request

    def approve(self, decision):
        s = self.load()
        if s['stage'] != 'awaiting_f10_review':
            raise RuntimeError('Run is not awaiting F10 review')
        req = json.loads((self.path / 'review_request.json').read_text(encoding='utf-8'))
        from .bios import expected_changes
        unsigned = {k:v for k,v in req.items() if k != 'request_sha256'}
        if object_sha(unsigned) != req['request_sha256'] or req['expected_changes'] != expected_changes(s['profile'], s['slot']):
            raise RuntimeError('Review request integrity mismatch')
        if req['profile_sha256'] != object_sha(s['profile']) or req['slot'] != s['slot']:
            raise RuntimeError('Plan changed since review request')
        for item in req['evidence']:
            if sha(self.path / item['path']) != item['sha256']:
                raise RuntimeError('Review evidence changed')
        if (decision.get('request_sha256') != req['request_sha256']
                or decision.get('observed_changes') != req['expected_changes']
                or decision.get('all_pages_reviewed') is not True
                or decision.get('decision') != 'approve'
                or not decision.get('reviewer') or not decision.get('reason')):
            raise RuntimeError('Review does not approve the exact complete change list')
        if req.get('expected_profile_storage'):
            if (decision.get('profile_storage_verified') is not True
                    or not any(x['label'].startswith('profile8_saved') for x in req['evidence'])):
                raise RuntimeError('Reviewer must verify Profile8 agent-tune was saved')
        (self.path / 'review_decision.json').write_text(json.dumps(decision, indent=2, ensure_ascii=False), encoding='utf-8')
        self.update(stage='f10_approved', approved_request=req['request_sha256'])

    def consume_approval(self):
        s = self.load()
        if s['stage'] != 'f10_approved':
            raise RuntimeError('F10 approval required')
        decision = json.loads((self.path / 'review_decision.json').read_text(encoding='utf-8'))
        # Re-run the complete binding validation at the last possible moment.
        self.update(stage='awaiting_f10_review')
        self.approve(decision)
        # Mark before HID: a crashed confirmation is ambiguous, never replay it.
        self.update(stage='save_confirmation_sent')
