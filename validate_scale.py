"""Exercise the real SQLite allocation policy in fresh databases; never submit a paid job."""
import json, sqlite3, tempfile
from pathlib import Path
import server, shot_tasks

def capacity(cuts, workers, route='story', lengths=None):
    with tempfile.TemporaryDirectory() as tmp:
        server.DATA=Path(tmp);server.setup()
        with server.db() as c:
            shots=[dict(prompt=f'镜 {i+1}',seconds=sec,cut=cut) for i,(cut,sec) in enumerate(zip(cuts,lengths or [15]*len(cuts)))]
            c.execute("INSERT INTO jobs(id,prompt,shots,seconds,state,route,lease,wid) VALUES('film','测试',?,?,'running',?,'epoch','drive/main')",(json.dumps(shots),sum(s['seconds'] for s in shots),route))
            job=c.execute('SELECT * FROM jobs').fetchone()
            shot_tasks.offer(c,job);picked=[]
            for i in range(1,workers):
                claim=shot_tasks.claim(c,'drive/'+str(i),{'shot_v1':True},100,server.jobrow)
                if claim:picked.append(claim['ix'])
            assert len(picked)==len(set(picked))
            return [0,*picked]

def run():
    assert capacity([False,True],4)==[0], '不足三段不要派副卡'
    assert capacity([False,False,True],4)==[0,2]
    assert capacity([False,True,True],4)==[0,1,2]
    assert capacity([False,True,True,True],4)==[0,1,2,3]
    assert len(capacity([False]+[True]*7,7))==6,'即使七张卡在线，同片也最多六张'
    assert capacity([False,True,True],4,'continuous')==[0]
    assert capacity([False]*5,4)==[0]
    for n in (2,3,4):
        print('可用卡数',n,'｜原三镜实用',len(capacity([False,False,True],n)),'｜四个独立镜头实用',len(capacity([False,True,True,True],n)))
if __name__=='__main__':run()

def available_work():
    """The main runtime must pick shot 3 while shot 2 is being rendered by a helper."""
    import ast,hashlib
    class Assemble(Exception):pass
    with tempfile.TemporaryDirectory() as td:
        root=Path(td);order=[];poll=[]
        def rpc(route,data):
            if data['progress']==92:raise Assemble()
        def take(job,i,path,piece,update):
            if i!=1:return False
            poll.append(i)
            if 2 not in order:return None
            piece.write_text('helper');return True
        def piece(job,i,path,dest,prev,update):
            assert prev is None or prev.is_file()
            order.append(i);dest.write_text(str(i))
        tree=ast.parse(Path(__file__).with_name('colab-worker.py').read_text())
        fn=next(n for n in tree.body if getattr(n,'name','')=='render_job')
        env=dict(ACTIVE_DIR=root,videos=root,hashlib=hashlib,rpc=rpc,dimensions=lambda job:(768,448),take_assisted_piece=take,render_piece=piece,restore_pieces=lambda job,pieces,update:{i for i,p in enumerate(pieces) if not p.is_file()})
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<worker>','exec'),env)
        try:env['render_job'](dict(id='x',lease_token='t',seconds=60,parallel_cuts=True,shots=[{'seconds':15,'cut':flag} for flag in [False,True,True,False]]))
        except Assemble:pass
        assert order==[0,2,3] and len(poll)>=2
        assert [p.read_text() for p in sorted(root.glob('*/*.mp4'))]==['0','helper','2','3']
        act=root/'jobs';vids=root/'videos';act.mkdir();vids.mkdir();done=[]
        env.update(ACTIVE_DIR=act,videos=vids,rpc=lambda r,d:done.append(d),drive_result=lambda p:'https://drive.google.com/file/d/abcdefghij/view',
                   h3_music_policy=lambda *a:(False,'orig'),ff=lambda *a:Path(a[-1]).write_bytes(b'mp4'))
        env['render_job'](dict(id='donejob',lease_token='t',prompt='海',seconds=15,shots=[{'seconds':15,'prompt':'海'}]))
        assert not list(act.iterdir()) and len(list(vids.glob('studio-*.mp4')))==1 and done[-1]['state']=='done'
if __name__=='__main__':available_work()
