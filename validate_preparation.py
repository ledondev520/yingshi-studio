"""Real file measurements and authenticated HTTP; no model downloads or GPU calls."""
import json,tempfile,threading,time,urllib.request,urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import server,preparation
from autowake.preparation_client import Preparation,close_all

def run():
 with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);server.DATA=server.KEYS=root;server.setup();(root/'worker-key').write_text('prep-test-only')
    http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);threading.Thread(target=http.serve_forever,daemon=True).start()
    url='http://127.0.0.1:'+str(http.server_port);t=int(time.time())
    def post(body,key='prep-test-only',status=200):
        req=urllib.request.Request(url+'/api/worker/preparation',data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
        try:r=urllib.request.urlopen(req,timeout=5)
        except urllib.error.HTTPError as e:r=e
        assert r.code==status,(r.code,r.read())
    base=dict(worker_id='images',gpu='NVIDIA A100-SXM4-80GB',memory_mib=81920,precision='image-bf16')
    try:
        with patch.object(threading.Thread,'start'):
            client=Preparation(url,{'worker_id':'images','worker_key':'prep-test-only'},'image-bf16',base['gpu'],81920)
        (root/'one.safetensors').write_bytes(b'a'*7)
        (root/'two.safetensors.incomplete').write_bytes(b'b'*11)
        client.weights(root,{'one.safetensors':7,'two.safetensors':11,'config.json':9})
        snapshot=client.snapshot();assert (snapshot['bytes_done'],snapshot['bytes_total'],snapshot['files_done'],snapshot['files_total'],snapshot['reused_bytes'])==(7,18,1,2,7)
        post({**base,**snapshot},key='wrong',status=403)
        post({**base,**snapshot,'bytes_done':19},status=400)
        post({**base,**snapshot,'stage':'fake'},status=400)
        with server.db() as c:
            for mode,device in [('image-bf16','images'),('quantized','videos')]:
                c.execute('insert into pool_signal values(?,?,?,?)',(device,mode,'loading',t+600))
                c.execute("insert into jobs(id,state,shots,precision,created,updated,tries) values(?,'queued','[]',?,?,?,0)",(device,mode,t,t))
        client.report(snapshot)
        post(dict(base,worker_id='videos',precision='quantized',stage='environment',bytes_done=0,bytes_total=0,files_done=0,files_total=0,reused_bytes=0))
        with server.db() as c:
            assert c.execute('select count(*) from workers').fetchone()[0]==0,'Preparing workers cannot claim jobs as ready'
            image=server.jobrow(c.execute("select * from jobs where id='images'").fetchone(),c=c)
            video=server.jobrow(c.execute("select * from jobs where id='videos'").fetchone(),c=c)
            assert image['preparation'][0]['stage']=='download' and image['preparation'][0]['bytes_done']==7
            assert video['preparation'][0]['stage']=='environment'
            assert len(server.fleet.cluster_summary(c,t)['preparation'])==2
            c.execute('update pool_preparation set seen=? where device=?',(t-100,'images'))
            assert preparation.visible(c,t,'image-bf16')[0]['stale']
        (root/'two.safetensors').write_bytes(b'b'*11);assert client.snapshot()['bytes_done']==18
        client.weights(root,{'one.safetensors':7,'two.safetensors':11});assert client.snapshot()['stage']=='cache' and client.snapshot()['reused_bytes']==18
        client.stage('loading');client.report(client.snapshot())
        with server.db() as c:
            p=preparation.visible(c,t,'image-bf16')[0];assert p['stage']=='loading' and p['bytes_total']==0
        client.stage('ready');client.report(client.snapshot())
        with server.db() as c:assert not preparation.visible(c,t,'image-bf16')
        close_all(failed=True)
        with server.db() as c:assert preparation.visible(c,t,'image-bf16')[0]['stage']=='failed'
        assert client.stop.is_set(),'A failed startup must stop its progress heartbeat'
        print('PASS cached bytes, partial-file exclusion, phase transitions, auth, mode isolation, stale feedback, no premature readiness')
    finally:http.shutdown();http.server_close()

if __name__=='__main__':run()
