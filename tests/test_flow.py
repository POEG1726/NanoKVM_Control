import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from nanokvm_flow.bios import expected_changes
from nanokvm_flow.state import Run
from nanokvm_flow.workflow import wait_video, confirm_save


class Fake:
    def __init__(self): self.pressed=[]
    async def keys(self, keys): self.pressed.append(keys)
    async def screenshot(self): return b'fake-jpeg', {'ok':True}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.profile=json.loads((Path(__file__).parents[1]/'profiles/b850m_f12d.json').read_text())
        self.run=Run(self.tmp.name,'sample'); self.run.create(self.profile,0)
        self.run.image(b'profile-saved-list','profile8_saved')
        self.run.image(b'fake-jpeg','f10_change_list')
        self.req=self.run.review_request(expected_changes(self.profile,0))
        self.decision={'decision':'approve','reviewer':'test-agent','reason':'Entire single-row list matches',
                       'request_sha256':self.req['request_sha256'],
                       'observed_changes':self.req['expected_changes'],'all_pages_reviewed':True,
                       'profile_storage_verified':True}

    def test_save_blocked_before_review(self):
        client=Fake()
        with self.assertRaises(RuntimeError): asyncio.run(confirm_save(client,self.run))
        self.assertEqual(client.pressed,[])

    def test_wait_video_cannot_replace_review_stage(self):
        for stage in ('awaiting_f10_review','f10_approved','navigating','waiting_windows'):
            self.run.update(stage=stage)
            with self.assertRaises(RuntimeError): asyncio.run(wait_video(Fake(),self.run,send_delete=True))
            self.assertEqual(self.run.load()['stage'],stage)

    def test_changed_request_rejected_even_with_old_hash(self):
        req=copy.deepcopy(self.req);req['expected_changes'][0]['to_mv']=1100
        (self.run.path/'review_request.json').write_text(json.dumps(req))
        decision=copy.deepcopy(self.decision);decision['observed_changes']=req['expected_changes']
        with self.assertRaises(RuntimeError): self.run.approve(decision)

    def test_no_extra_change_can_be_approved(self):
        self.decision['observed_changes'].append({'parameter':'vsoc','to_mv':1250})
        with self.assertRaises(RuntimeError): self.run.approve(self.decision)

    def test_changed_evidence_after_approval_blocks_hid(self):
        self.run.approve(self.decision)
        (self.run.path/self.req['evidence'][0]['path']).write_bytes(b'changed')
        client=Fake()
        with self.assertRaises(RuntimeError): asyncio.run(confirm_save(client,self.run))
        self.assertEqual(client.pressed,[])

    def test_changed_slot_after_approval_blocks_hid(self):
        self.run.approve(self.decision);self.run.update(slot=1)
        with self.assertRaises(RuntimeError): asyncio.run(confirm_save(Fake(),self.run))

    def test_save_is_one_shot(self):
        self.run.approve(self.decision);client=Fake()
        asyncio.run(confirm_save(client,self.run))
        self.assertEqual(client.pressed,[['Enter']])
        self.assertEqual(self.run.load()['stage'],'waiting_windows')
        with self.assertRaises(RuntimeError): asyncio.run(confirm_save(client,self.run))

    def test_dsview_start_is_requested_before_hid_without_gating_on_capture_failure(self):
        self.run.approve(self.decision)
        order=[]
        class Client(Fake):
            async def keys(self,keys):
                order.append('hid');await super().keys(keys)
        def start(*args):
            order.append('capture_request');raise RuntimeError('fixture capture unavailable')
        client=Client()
        with patch('nanokvm_flow.dsview.ensure_capture_started',side_effect=start), patch('nanokvm_flow.hdmi_watch.start_background'):
            asyncio.run(confirm_save(client,self.run,{'notifications':{}}))
        self.assertEqual(order,['capture_request','hid'])
        self.assertEqual(self.run.load()['stage'],'waiting_windows')
        timing=json.loads((self.run.path/'boot-timing.json').read_text())
        self.assertEqual(timing['origin'],'f10_hid_command')
        self.assertIsNotNone(timing['start']['command_start_monotonic_ns'])
        self.assertGreaterEqual(timing['start']['ack_monotonic_ns'],timing['start']['command_start_monotonic_ns'])
        self.assertTrue(list((self.run.path/'notifications').glob('*.json')))


if __name__=='__main__':unittest.main()
