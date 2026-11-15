"""Test reservation races, allocation denial, independent execution and recovery."""
import json,sys,tempfile,threading,time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock,patch
import controller as m
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server
def run():
 with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);server.DATA=root;server.setup();m.DB=root/'studio.sqlite';m.PRIVATE=root;m.TOKEN=root/'token';m.TOKEN.touch()
    with m.database() as c:
        m.setup(c)
        c.execute("insert into jobs(id,shots,seconds,state,route,quality,prompt,created) values('j',?,80,'queued','story','standard','t',1)",(json.dumps([{'seconds':10,'prompt':'a','cut':i>0} for i in range(8)]),))
        c.execute("insert into workers values('manual/0','A100',?)",(int(time.time()),));m.fleet.register(c,'manual/0',dict(shot_v1=True,relay_v1=True,finish_v2=True))
    cl=Mock();seen=set();winner={};attempts={};l=threading.Lock()
    barrier=threading.Barrier(5)
    def assign(stable,variant,model,highmem=False):
        with l:
            seen.add(stable);attempts.setdefault(stable,[]).append(model.value)
            first=stable==winner.setdefault(0,stable)
        if model==m.Accelerator.A100:barrier.wait(timeout=5)
        if not first or model!=m.Accelerator.G4:raise m.Capacity('unavailable')
        return Mock(endpoint='runtimes/h3-'+stable.hex[:24])
    cl.assign.side_effect=assign
    with patch.object(m,'StateStore'),patch.object(m,'creds'),patch.object(m,'PublicColab',return_value=cl),patch.object(m.Slot,'initialize'),patch.object(m.Slot,'register'):
        slots=[m.Slot(i) for i in range(6)]
        with ThreadPoolExecutor(6) as ex:list(ex.map(lambda s:s.step(),slots))
        assert len(seen)==5 and sum(bool(s.cache.get('endpoint')) for s in slots)==1,seen
        assert attempts[winner[0]]==['A100','H100','G4']
        assert all(seq==['A100','H100','G4'] for ident,seq in attempts.items() if ident!=winner[0]),attempts
        with m.database() as c:
            assert c.execute("select count(*) from pool_retry").fetchone()[0]==1
            # The existing parent claims an open cut for local rendering immediately.
            c.execute("update jobs set state='running',wid='manual/0',lease='live',deadline=?",(int(time.time())+600,))
            row=c.execute("select * from jobs").fetchone();server.shot_tasks.offer(c,row)
            for ix in range(1,8):assert server.shot_tasks.handle(c,'status','manual/0',dict(id='j',ix=ix,lease_token='live'),int(time.time()),server.fail)=={'state':'local'}
            assert not m.fleet.need(c,int(time.time()))
        for slot in slots:slot.step()
        assert len(seen)==5
        # Explicitly owned and idle runtime only. Busy blocks release.
        owned=slots[0];owned.cache.update(endpoint='mine',last=0,mode='quantized',born=1);cl.get.return_value=Mock(endpoint='mine')
        with m.database() as c:
            c.execute("update jobs set wid=?",(owned.device+'/a',));c.execute("insert into workers values(?, 'A100',?)",(owned.device+'/a',int(time.time())))
        owned.release(cl,'mine');cl.unassign.assert_not_called()
        with m.database() as c:c.execute("update jobs set state='done',updated=0")
        owned.step();cl.unassign.assert_called_once_with('mine')
 print('PASS concurrent cold reservations, idle-first, capacity denial, single-card completion, owned-only TTL release')
if __name__=='__main__':run()

def multiuser_pool():
 """Every waiting video can reserve its own slot, without duplicate allocations."""
 with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);server.DATA=root;server.setup();m.DB=root/'studio.sqlite';m.PRIVATE=root;m.TOKEN=root/'token';m.TOKEN.touch()
    with m.database() as c:
        m.setup(c)
        for i in range(32):
            c.execute("insert into jobs(id,shots,seconds,state,route,quality,prompt,created) values(?, ?,15,'queued','continuous','standard','t',?)",(str(i),json.dumps([{'seconds':15,'prompt':'a'}]),i))
    cl=Mock()
    cl.assign.side_effect=lambda stable,*args,**kw:Mock(endpoint='runtimes/h3-'+stable.hex[:24])
    with patch.object(m,'StateStore'),patch.object(m,'creds'),patch.object(m,'PublicColab',return_value=cl),patch.object(m.Slot,'initialize') as start,patch.object(m.Slot,'register'):
        slots=[m.Slot(i) for i in range(m.fleet.MAX_CARDS)]
        with ThreadPoolExecutor(10) as ex:list(ex.map(lambda slot:slot.step(),slots))
        assert len({slot.cache.get('endpoint') for slot in slots})==32
        assert cl.assign.call_count==start.call_count==32
        for slot in slots:slot.step()
        assert cl.assign.call_count==32,'Restarting a poll must not allocate duplicate GPUs'
 print('PASS 32 independent provisioning reservations, repeated polls allocate no extra runtimes')
if __name__=='__main__':multiuser_pool()
