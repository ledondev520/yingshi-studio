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
        if model==m.Accelerator.H100:barrier.wait(timeout=5)
        if not first or model!=m.Accelerator.A100:raise m.Capacity('unavailable')
        return Mock(endpoint='runtimes/h3-'+stable.hex[:24])
    cl.assign.side_effect=assign
    with patch.object(m,'StateStore'),patch.object(m,'creds'),patch.object(m,'PublicColab',return_value=cl),patch.object(m.Slot,'initialize'),patch.object(m.Slot,'register'):
        slots=[m.Slot(i) for i in range(6)]
        with ThreadPoolExecutor(6) as ex:list(ex.map(lambda s:s.step(),slots))
        assert len(seen)==5 and sum(bool(s.cache.get('endpoint')) for s in slots)==1,seen
        assert attempts[winner[0]]==['H100','A100']
        assert all(seq==['H100','A100','G4'] for ident,seq in attempts.items() if ident!=winner[0]),attempts
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
        assert [call.args[2] for call in cl.assign.call_args_list]==[m.Accelerator.H100,m.Accelerator.A100,m.Accelerator.G4]
        with m.database() as c:
            assert m.fleet.cluster_summary(c,int(time.time()))['runtime_limit']
            assert c.execute('select stage from pool_signal').fetchone()[0]=='limit'
 print('PASS planning preflight, H100/A100/G4 fast-first order, limit feedback survives backoff')
if __name__=='__main__':quota_feedback()

def runtime_recovery():
 """A confirmed missing runtime releases work; a transient API error does not."""
 with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);server.DATA=root;server.setup();m.DB=root/'studio.sqlite';m.PRIVATE=root;m.TOKEN=root/'token';m.TOKEN.touch()
    t=2000;cl=Mock()
    with patch.object(m,'StateStore'),patch.object(m,'creds'),patch.object(m,'PublicColab',return_value=cl),patch.object(m.time,'time',return_value=t),patch.object(m.Slot,'initialize') as start,patch.object(m.Slot,'register'):
        slot=m.Slot(0);slot.cache.update(endpoint='runtimes/lost',mode='quantized',born=100,last=1800,launched=100,retry=t+300);slot.persist()
        with m.database() as c:
            m.setup(c)
            for jid,wid in [('lost',slot.device+'/old'),('safe',slot.device+'-2/healthy')]:
                c.execute("insert into jobs(id,shots,seconds,state,route,prompt,wid,lease,deadline,updated,tries,progress) values(?,'[]',15,'running','continuous','t',?,'old-lease',?, ?,1,70)",(jid,wid,t+500,t-120))
            c.execute("insert into shot_checkpoints values('lost',0,'plan','sha',12,100)")
            c.execute("insert into shot_tasks values('safe','old-lease',1,'taken',?,'ticket',?,null)",(slot.device+'/old',t+50))
        error=RuntimeError('temporary');error.response=Mock(status_code=503);cl.get.side_effect=error
        try:slot.step()
        except RuntimeError:pass
        else:raise AssertionError('Transient failures must retain the runtime for retry')
        with m.database() as c:assert c.execute("select state from jobs where id='lost'").fetchone()[0]=='running'
        assert slot.cache['endpoint']=='runtimes/lost';cl.assign.assert_not_called()
        error.response.status_code=404;slot.step()
        with m.database() as c:
            row=c.execute("select state,lease,wid,deadline,progress from jobs where id='lost'").fetchone()
            assert tuple(row)==('queued',None,None,None,0),'Confirmed 404 must not wait for the ten-minute job lease'
            assert c.execute("select state from jobs where id='safe'").fetchone()[0]=='running'
            assert tuple(c.execute("select state,helper,ticket from shot_tasks").fetchone())==('open',None,None)
            assert c.execute('select count(*) from shot_checkpoints').fetchone()[0]==1
        assert not slot.cache.get('endpoint') and slot.cache.get('retry',0)<=t
        cl.get.side_effect=None;cl.assign.return_value=Mock(endpoint='runtimes/replacement')
        slot.step();cl.assign.assert_called_once();start.assert_called_once()
        slot.step();cl.assign.assert_called_once()
        # A surviving runtime with an idle/dead worker must be restarted even while its job lease is valid.
        with m.database() as c:
            c.execute("update jobs set state='running',wid=?,lease='new',deadline=? where id='lost'",(slot.device+'/new',t+500))
        slot.cache['launched']=t-200;start.reset_mock();slot.step();start.assert_called_once()
        cl.unassign.assert_not_called()
 print('PASS confirmed 404 requeues only affected work, retains checkpoints, retries without delay, and revives silent workers')
if __name__=='__main__':runtime_recovery()

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

def connection_recovery():
 """Fresh worker heartbeats must not suppress kernel reconnection after a deploy."""
 from types import SimpleNamespace
 with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);server.DATA=root;server.setup();m.DB=root/'studio.sqlite';m.PRIVATE=root;m.TOKEN=root/'token';m.TOKEN.touch()
    t=int(time.time());cl=Mock();state=SimpleNamespace(endpoint='runtimes/live',kernel_id='existing',url='https://runtime.invalid',token='test')
    links=[]
    def connect(s,kid):
        assert kid=='existing';link=Mock(endpoint=s.endpoint);link.is_alive.return_value=True;links.append(link);return link
    with patch.object(m,'StateStore'),patch.object(m,'creds'),patch.object(m,'PublicColab',return_value=cl),patch.object(m.Slot,'initialize') as launch,patch.object(m.Slot,'register',return_value=state),patch.object(m,'KernelLink',side_effect=connect,create=True),patch.object(m.requests,'get',create=True) as get:
        get.return_value.json.return_value=[{'id':'existing','execution_state':'busy'}]
        slot=m.Slot(0);slot.cache.update(endpoint='runtimes/live',mode='quantized',born=t-300,last=t,sent=['j']);slot.persist()
        with m.database() as c:
            m.setup(c)
            c.execute("insert into workers values(?,'A100',?)",(slot.device+'/gpu',t))
            c.execute("insert into jobs(id,shots,seconds,state,route,prompt,wid,lease,deadline) values('j','[]',15,'running','continuous','t',?,'lease',?)",(slot.device+'/gpu',t+600))
        slot.step();assert len(links)==1,'An online worker still needs its execution connection restored after controller restart'
        slot.step();assert len(links)==1
        links[0].is_alive.return_value=False;slot.step();assert len(links)==2;links[0].close.assert_called_once()
        restarted=m.Slot(0);restarted.step();assert len(links)==3
        with m.database() as c:assert c.execute("select state from jobs where id='j'").fetchone()[0]=='running'
        launch.assert_not_called();cl.assign.assert_not_called();cl.unassign.assert_not_called()
 print('PASS controller restart and transport loss reconnect the busy kernel without duplicate execution or allocation')
if __name__=='__main__':connection_recovery()

def preempt_and_orphan_reclaim():
 """Quota exhaustion sweeps orphan h3-* VMs (never user notebooks) and preempts idle cross-mode slots immediately."""
 from types import SimpleNamespace
 with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);server.DATA=root;server.setup();m.DB=root/'studio.sqlite';m.PRIVATE=root;m.TOKEN=root/'token';m.TOKEN.touch()
    t=int(time.time());cl=Mock()
    orphan_ep='runtimes/h3-'+'a'*24
    busy_ep='runtimes/h3-'+'b'*24
    user_notebook_ep='runtimes/m-s-manual-user-notebook'
    freed=[]
    def list_rt():
        active=[SimpleNamespace(endpoint=busy_ep),SimpleNamespace(endpoint=user_notebook_ep)]
        if orphan_ep not in freed:active.append(SimpleNamespace(endpoint=orphan_ep))
        return active
    def unassign(ep):
        freed.append(ep);return {}
    def assign(stable,variant,model,**kw):
        if orphan_ep not in freed:raise m.Capacity('TOO_MANY_ACTIVE_RUNTIMES')
        return SimpleNamespace(endpoint='runtimes/h3-'+stable.hex[:24],accelerator=SimpleNamespace(value=model.value))
    cl.list_assignments.side_effect=list_rt
    cl.unassign.side_effect=unassign
    cl.assign.side_effect=assign
    cl.get.side_effect=lambda ep:SimpleNamespace(endpoint=ep,accelerator=SimpleNamespace(value='H100'))
    with patch.object(m,'StateStore'),patch.object(m,'creds'),patch.object(m,'PublicColab',return_value=cl),patch.object(m.Slot,'initialize') as start,patch.object(m.Slot,'register'),patch.object(m.Slot,'attach'):
        s_busy=m.Slot(0);s_busy.cache.update(endpoint=busy_ep,mode='quantized',born=t-100,last=t);s_busy.persist()
        s_idle_img=m.Slot(1);s_idle_img.cache.update(endpoint='runtimes/h3-'+'c'*24,mode='image-bf16',born=t-20,last=t-5);s_idle_img.persist()
        s_new=m.Slot(2)
        with m.database() as c:
            m.setup(c)
            c.execute("insert into workers values(?,'H100',?)",(s_busy.device+'/gpu',t))
            c.execute("insert into workers values(?,'A100',?)",(s_idle_img.device+'/gpu',t))
            c.execute("insert into worker_modes values(?,'image-bf16')",(s_idle_img.device+'/gpu',))
            c.execute("insert into jobs(id,shots,seconds,state,route,prompt,wid,lease,deadline) values('run1','[]',15,'running','continuous','t',?,'l1',?)",(s_busy.device+'/gpu',t+600))
            c.execute("insert into jobs(id,shots,seconds,state,route,prompt) values('q1','[]',15,'queued','continuous','t')")
            c.execute("insert into jobs(id,shots,seconds,state,route,prompt) values('q2','[]',15,'queued','continuous','t')")
        # 1) s_new hits TOO_MANY_ACTIVE_RUNTIMES -> sweeps orphan_ep (preserving busy_ep and user_notebook_ep!) and immediately allocates!
        s_new.step()
        assert freed==[orphan_ep],'Must evict only unowned h3-* orphan and preserve manual user notebook + busy slot'
        assert s_new.cache.get('endpoint')=='runtimes/h3-'+s_new.cache['notebook'].replace('-','')[:24]
        start.assert_called_once()
        # 2) q2 still needs a quantized GPU -> s_idle_img (idle only 5s ago, well below IDLE=300s!) immediately preempts itself!
        s_idle_img.step()
        assert 'runtimes/h3-'+'c'*24 in freed,'Idle image-bf16 slot must immediately release for queued video job'
        assert not s_idle_img.cache.get('endpoint')
 print('PASS orphan h3-* sweep (preserving manual user notebooks) and immediate cross-mode preemption')
if __name__=='__main__':preempt_and_orphan_reclaim()
