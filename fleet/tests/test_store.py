import concurrent.futures
import tempfile
from pathlib import Path
import unittest
from fleet.store import Store,validate

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.s=Store(Path(self.tmp.name)/'jobs.sqlite')
        self.s.peers_configure([{'id':'a','capacity':{'cpu':4,'ram_mb':4096,'gpu_mb':1024},'max_jobs':3,'ram_headroom_mb':512,'interactive_ram_mb':256,'interactive_cpu':1}])
        self.s.observe('a',{'reachable':True,'profiles':['diagnostic','render-gpu'],'ram_available_mb':4096,'gpu_free_mb':1024,'cpu_busy_pct':10})
    def job(self,agent='master'):
        return {'profile':'diagnostic','resources':{'cpu':1,'ram_mb':256,'gpu_mb':0},'parent':{'host':'home','agent':agent,'task':'one'}}
    def test_parallel_reservation_never_overbooks(self):
        for n in range(12):self.s.submit(self.job(str(n)))
        with concurrent.futures.ThreadPoolExecutor(8) as pool:results=list(pool.map(lambda _:self.s.reserve_next(),range(12)))
        self.assertEqual(sum(r is not None for r in results),3)
    def test_idempotency_fencing_and_acknowledgement(self):
        j=self.s.submit(self.job(),'request-1');self.assertEqual(self.s.submit(self.job(),'request-1'),j)
        with self.assertRaises(ValueError):self.s.submit(self.job('another'),'request-1')
        x=self.s.reserve_next();self.assertFalse(self.s.transition(j,'stale','succeeded',{}))
        self.assertTrue(self.s.transition(j,x['attempt'],'succeeded',{'ok':True}))
        with self.assertRaises(ValueError):self.s.acknowledge(j,{'host':'other'})
        self.s.acknowledge(j,self.job()['parent']);self.assertIsNotNone(self.s.get(j)['acknowledged'])
        self.assertFalse(self.s.transition(j,x['attempt'],'running'))
    def test_stale_drained_and_unknown_memory_are_ineligible(self):
        self.s.drain('a',True);self.assertFalse(self.s.choices(self.job())[0]['eligible']);self.s.drain('a',False)
        self.s.observe('a',{'reachable':True,'profiles':['diagnostic']});self.assertIn('RAM availability unknown',self.s.choices(self.job())[0]['reasons'])
        with self.s.db() as c:c.execute('UPDATE peers SET observed=0')
        self.assertIn('stale telemetry',self.s.choices(self.job())[0]['reasons'])
    def test_cancel_queued_and_active(self):
        j=self.s.submit(self.job());self.assertEqual(self.s.cancel(j)['status'],'cancelled');self.assertIsNone(self.s.reserve_next())
        j=self.s.submit(self.job());self.s.reserve_next();self.assertEqual(self.s.cancel(j)['status'],'cancelling')
    def test_bad_resources_and_interactive_bypass_rejected(self):
        with self.assertRaises(ValueError):validate({**self.job(),'resources':{'cpu':float('nan')}})
        with self.assertRaises(ValueError):validate({**self.job(),'profile':'cmake','priority':'interactive'})
    def test_gpu_slot_and_reserved_ram(self):
        j={**self.job(),'profile':'render-gpu','resources':{'cpu':1,'ram_mb':256,'gpu_mb':512}}
        self.s.submit(j);self.s.reserve_next();self.assertIn('GPU execution slot occupied',self.s.choices(j)[0]['reasons'])
    def test_restart_preserves_result_and_reservation(self):
        j=self.s.submit(self.job());x=self.s.reserve_next();restored=Store(self.s.path)
        self.assertEqual(restored.get(j)['attempt'],x['attempt']);self.assertEqual(restored.broker_id(),self.s.broker_id())

    def test_interactive_slot_survives_background_fanout(self):
        for n in range(3):self.s.submit(self.job(str(n)));self.s.reserve_next()
        request={**self.job('speech'),'priority':'interactive'}
        self.s.submit(request);self.assertIsNotNone(self.s.reserve_next())
    def test_deadline_and_limits_are_enforced(self):
        j=self.s.submit(self.job())
        with self.s.db() as c:c.execute('UPDATE jobs SET created=0 WHERE id=?',(j,))
        self.s.expire_queued();self.assertEqual(self.s.get(j)['status'],'failed')
        self.s.set_limits('a',{'cpu':1});self.assertFalse(self.s.choices(self.job())[0]['eligible'])
        with self.assertRaises(ValueError):self.s.set_limits('a',{'cpu':999})

    def test_operator_budget_survives_configuration_reload(self):
        self.s.set_limits('a',{'cpu':2,'interactive_cpu':1})
        self.s.peers_configure([{'id':'a','capacity':{'cpu':4,'ram_mb':4096,'gpu_mb':1024}}])
        self.assertEqual(self.s.snapshot()['peers'][0]['config']['capacity']['cpu'],2)
    def test_invalid_telemetry_fails_closed(self):
        self.s.observe('a',{'reachable':True,'profiles':['diagnostic'],'ram_available_mb':'lots'})
        self.assertFalse(self.s.choices(self.job())[0]['eligible'])

if __name__=='__main__':unittest.main()
