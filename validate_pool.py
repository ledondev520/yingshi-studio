"""Queue/worker integration using isolated SQLite. No Google allocation."""
import tempfile,json,time
from pathlib import Path
import server
def run():
 import fleet
 with tempfile.TemporaryDirectory() as tmp:
    server.DATA=Path(tmp);server.setup();t=int(time.time())
    with server.db() as c:
        c.execute("insert into jobs(id,shots,seconds,state,route,quality,prompt,created) values('j',?,80,'queued','story','standard','海边',1)",(json.dumps([{'seconds':10,'prompt':'海边','cut':i>0} for i in range(8)]),))
        c.execute("update jobs set state='planning'")
        assert fleet.need(c,t)==['quantized'],'编排期间即准备一张主卡'
        c.execute("update jobs set state='queued'")
        assert fleet.need(c,t)==['quantized']*6
        for i in range(6):
            wid='manual/'+str(i)
            c.execute('insert into workers values(?,?,?)',(wid,'A100',t))
            fleet.register(c,wid,dict(shot_v1=True,relay_v1=True,finish_v2=True,append_v1=True,turbo8=True))
            assert len(fleet.need(c,t))==5-i
        c.execute("update jobs set state='running',wid='manual/0',lease='x',deadline=?",(t+600,))
        r=c.execute('select * from jobs').fetchone();server.shot_tasks.offer(c,r)
        c.execute("delete from workers where id!='manual/0'")
        assert len(fleet.need(c,t))==5
        a=server.shot_tasks.claim(c,'managed/1',dict(shot_v1=True,relay_v1=True),t,server.jobrow)
        assert a and a['relay'] and 'lease_token' not in a['job']
        assert len(fleet.need(c,t))==4
        c.execute("update shot_tasks set state='local' where state='open'")
        assert not fleet.need(c,t),'本机接手后不能再申请'
        c.execute("update jobs set state='queued',precision='bf16',route='continuous',wid=NULL")
        assert fleet.need(c,t)==['bf16']
        c.execute("insert into pool_signal values('new-card','bf16','loading',?)",(t+300,))
        c.execute("insert into pool_signal values('limited-card','bf16','limit',?)",(t+300,))
        summary=fleet.cluster_summary(c,t)
        assert summary['loading_count']==1 and summary['runtime_limit']
        c.execute("delete from pool_signal where stage='loading'")
        assert '活动运行时已达上限' in fleet.wait_message(c,'bf16',t)
 print('PASS idle first, both precisions, 6-card cap, cross-machine lease and disappearing demand')
if __name__=='__main__':run()
