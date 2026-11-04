import copy
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from nanokvm_flow.dsview import ensure_capture_started, stop_capture, DSViewError


class DSViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.run=SimpleNamespace(name='run1',path=self.root)
        self.config={'enabled':True,'output_root':str(self.root/'captures'),'format':'csv',
                     'growth_timeout_seconds':2,'growth_poll_seconds':.5}
        self.active={'ok':True,'schema_version':1,'working':True,'device_collecting':True,
                     'recorder_running':True,'recorder_failed':False,'loop_mode':True,'stream_mode':True,
                     'recorder_ready':True,'header_ready':True,
                     'sample_rate_hz':50000000,'format':'csv','output_directory':str(self.root/'recording'),
                     'samples_written':64,'bytes_written':100,'queued_bytes':0,'start_requested':True}
        self.clock=0
    def sleep(self,seconds):self.clock+=seconds
    def time(self):return self.clock
    def growth(self):
        state=copy.deepcopy(self.active);state.update(samples_written=128,bytes_written=120);return state
    def test_start_returns_without_followup_status_or_growth_wait(self):
        calls=[]
        def call(cfg,op,directory=None):
            calls.append(op)
            return {'working':False,'recorder_running':False} if len(calls)==1 else copy.deepcopy(self.active) if op=='start' else self.growth()
        sizes=iter([100,120])
        result=ensure_capture_started(self.config,self.run,call=call,sleep=self.sleep,clock=self.time,disk_bytes=lambda d:next(sizes))
        self.assertFalse(result['reused']);self.assertEqual(calls,['status','start'])
        self.assertEqual(result['status'],'capture_start_requested')
        self.assertEqual(self.clock,0)
    def test_running_capture_reused_without_toggle(self):
        calls=[]
        def call(cfg,op,directory=None):calls.append(op);return copy.deepcopy(self.active) if len(calls)==1 else self.growth()
        sizes=iter([100,120])
        result=ensure_capture_started(self.config,self.run,call=call,sleep=self.sleep,clock=self.time,disk_bytes=lambda d:next(sizes))
        self.assertTrue(result['reused']);self.assertEqual(calls,['status'])
    def test_observation_functions_are_never_called(self):
        def call(cfg,op,directory=None):return copy.deepcopy(self.active)
        def forbidden(*args):self.fail('start must not wait for observation evidence')
        ensure_capture_started(self.config,self.run,call=call,sleep=forbidden,clock=forbidden,disk_bytes=forbidden)
    def test_idle_espi_without_samples_header_or_semantic_growth_returns(self):
        self.config['format']='espi';self.active.update(format='espi',samples_written=0,bytes_written=0,
                                                     header_ready=False,recorder_ready=False)
        calls=[]
        def call(cfg,op,directory=None):
            calls.append(op);state=copy.deepcopy(self.active)
            return state
        result=ensure_capture_started(self.config,self.run,call=call,sleep=self.sleep,clock=self.time,disk_bytes=lambda d:100)
        self.assertEqual(calls,['status'])
        self.assertTrue(result['reused'])
    def test_start_request_can_return_before_device_collecting(self):
        state=copy.deepcopy(self.active);state.update(working=False,device_collecting=False,
                                                    samples_written=0,bytes_written=0,header_ready=False)
        calls=[]
        def call(cfg,op,directory=None):
            calls.append(op)
            return {'working':False,'recorder_running':False} if op=='status' else state
        result=ensure_capture_started(self.config,self.run,call=call)
        self.assertEqual(calls,['status','start'])
        self.assertEqual(result['status'],'capture_start_requested')
    def test_existing_recording_root_not_overwritten(self):
        (Path(self.config['output_root'])/self.run.name).mkdir(parents=True)
        calls=[]
        def call(cfg,op,directory=None):calls.append(op);return {'working':False,'recorder_running':False}
        with self.assertRaises(DSViewError):ensure_capture_started(self.config,self.run,call=call)
        self.assertEqual(calls,['status'])
    def test_incompatible_running_capture_not_touched(self):
        state=copy.deepcopy(self.active);state['sample_rate_hz']=10000000
        with self.assertRaises(DSViewError):ensure_capture_started(self.config,self.run,call=lambda *a:state)
    def test_stop_requires_real_stop_and_flush(self):
        state=copy.deepcopy(self.active);state.update(working=False,device_collecting=False,recorder_running=False,flushed=True)
        Path(state['output_directory']).mkdir()
        result=stop_capture(self.config,self.run,call=lambda *a:state)
        self.assertEqual(result['status'],'stopped_and_flushed')
        state['recorder_running']=True
        with self.assertRaises(DSViewError):stop_capture(self.config,self.run,call=lambda *a:state)
    def test_partial_file_prevents_complete_stop_claim(self):
        state=copy.deepcopy(self.active);state.update(working=False,device_collecting=False,recorder_running=False,flushed=True)
        folder=Path(state['output_directory']);folder.mkdir();(folder/'capture.csv.zst.part').touch()
        with self.assertRaises(DSViewError):stop_capture(self.config,self.run,call=lambda *a:state)


if __name__=='__main__':unittest.main()
