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
        assert attempts[winner[0]]==['A100','G4']
        assert all(seq==['A100','G4','H100'] for ident,seq in attempts.items() if ident!=winner[0]),attempts
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
        folders={'trashed11111':{'trashed':True},'canonical1111':{'trashed':False}};created=[];uploads=[]
        auth=Mock()
        def dget(url,params=None,timeout=0):
            r=Mock(status_code=200);r.raise_for_status=lambda:None
            if url.endswith('/files'):
                live=[{'id':k} for k,v in folders.items() if not v['trashed']]
                r.json=lambda:{'files':live[:1]};return r
            fid=url.rsplit('/',1)[-1]
            if fid not in folders:r.status_code=404;return r
            r.json=lambda:{'id':fid,'trashed':folders[fid]['trashed']};return r
        def dpost(url,params=None,json=None,headers=None,timeout=0):
            r=Mock(status_code=200,headers={'Location':'https://www.googleapis.com/upload/session'});r.raise_for_status=lambda:None
            if 'upload/' in url:uploads.append(json['parents'][0]);return r
            nid=f'created{len(created)+1:05d}';folders[nid]={'trashed':False};created.append(nid);r.json=lambda:{'id':nid};return r
        auth.get.side_effect=dget;auth.post.side_effect=dpost
        slots[1].cache['folder']='trashed11111';slots[1].persist()
        for s in slots:m.ticket(auth,'a'*32,s)
        assert not created and uploads==['canonical1111']*len(slots) and all(s.cache['folder']=='canonical1111' for s in slots)
        folders['canonical1111']['trashed']=True
        m.ticket(auth,'b'*32,slots[0])
        assert created==['created00001'] and slots[0].cache['folder']=='created00001'
 print('PASS 32 independent provisioning reservations, repeated polls allocate no extra runtimes, drive folder dedup')
if __name__=='__main__':multiuser_pool()

def quota_feedback():
 with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);server.DATA=root;server.setup();m.DB=root/'studio.sqlite';m.PRIVATE=root;m.TOKEN=root/'token';m.TOKEN.touch()
    with m.database() as c:
        m.setup(c)
        c.execute("insert into jobs(id,shots,seconds,state,route,prompt) values('j','[]',15,'planning','continuous','t')")
    cl=Mock()
    def refused(stable,variant,model,**kw):
        raise m.Capacity('NO_RUNTIMES' if model==m.Accelerator.H100 else 'TOO_MANY_ACTIVE_RUNTIMES')
    cl.assign.side_effect=refused
    with patch.object(m,'StateStore'),patch.object(m,'creds'),patch.object(m,'PublicColab',return_value=cl):
        slot=m.Slot(0);slot.step();slot.step()
        assert [call.args[2] for call in cl.assign.call_args_list]==[m.Accelerator.A100,m.Accelerator.G4,m.Accelerator.H100]
        with m.database() as c:
            assert m.fleet.cluster_summary(c,int(time.time()))['runtime_limit']
            assert c.execute('select stage from pool_signal').fetchone()[0]=='limit'
 print('PASS planning preflight, A100/G4/H100 order, limit feedback survives backoff')
if __name__=='__main__':quota_feedback()

def lost_runtime_reschedules():
 with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);server.DATA=root;server.setup();m.DB=root/'studio.sqlite';m.PRIVATE=root;m.TOKEN=root/'token';m.TOKEN.touch()
    cl=Mock();gone=RuntimeError('runtime removed');gone.response=Mock(status_code=404);cl.get.side_effect=gone
    with patch.object(m,'StateStore'),patch.object(m,'creds'),patch.object(m,'PublicColab',return_value=cl):
        slot=m.Slot(0);slot.cache.update(endpoint='gone',mode='quantized',born=1,last=1)
        with m.database() as c:
            m.setup(c)
            for ident,wid in [('ours',slot.device+'/gpu'),('other','other-device/gpu')]:
                c.execute("insert into jobs(id,shots,seconds,state,route,prompt,wid,lease,deadline,tries) values(?,'[]',15,'running','continuous','t',?,'lease',?,4)",(ident,wid,int(time.time())+600))
        slot.step()
        with m.database() as c:
            assert tuple(c.execute("select state,lease,wid,tries from jobs where id='ours'").fetchone())==('queued',None,None,4)
            assert c.execute("select state from jobs where id='other'").fetchone()[0]=='running'
    cl.assign.assert_not_called()
 print('PASS a confirmed missing runtime requeues its own job immediately without waiting for its lease')
if __name__=='__main__':lost_runtime_reschedules()
