from pathlib import Path
import hashlib,json,tempfile,threading,time,sys,urllib.request as u,urllib.error
from http.server import ThreadingHTTPServer
import server
def run(src):
 with tempfile.TemporaryDirectory() as tmp:
    server.DATA=server.KEYS=Path(tmp);server.setup();(Path(tmp)/'worker-key').write_text('private-test')
    http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);threading.Thread(target=http.serve_forever,daemon=True).start();url='http://127.0.0.1:'+str(http.server_port)+'/api/worker/'
    def post(w,route,**d):
        q=u.Request(url+route,data=json.dumps(dict(worker_id=w,runtime_id='a'*24,gpu=('NVIDIA RTX PRO 6000 Blackwell Server Edition' if w=='managed-2' else 'A100'),shot_v1=True,relay_v1=True,**d)).encode(),headers={'Authorization':'Bearer private-test','Content-Type':'application/json'});return json.load(u.urlopen(q))
    try:
        with server.db() as c:c.execute("insert into jobs(id,state,created,quality,seconds,prompt,shots) values('demo','queued',1,'standard',15,'海边',?)",(json.dumps([{'seconds':5,'prompt':'海','cut':i>0} for i in range(3)]),))
        j=post('manual','claim')['job'];a=post('managed-2','claim')['assist'];b=post('managed-3','claim')['assist'];assert a['ix']!=b['ix'] and a['relay']
        assert post('fourth','claim')['assist'] is None
        t=a['ticket'];args=dict(id='demo',ix=a['ix'],ticket=t);blob=src.read_bytes();digest=hashlib.sha256(blob).hexdigest()
        def transfer(w,method,code,lease='',sha=digest):
            r=u.Request(url+'media/shot/'+t,data=blob if method=='PUT' else None,method=method,headers={'Authorization':'Bearer private-test','X-H3-Worker-ID':w,'X-H3-GPU':('NVIDIA RTX PRO 6000 Blackwell Server Edition' if w=='managed-2' else 'A100'),'X-H3-Runtime':'a'*24,'X-H3-Lease':lease,'X-Content-SHA256':sha})
            try:resp=u.urlopen(r)
            except urllib.error.HTTPError as e:resp=e
            assert resp.code==code,(resp.code,code);return resp.read()
        transfer('fourth','PUT',403);transfer('managed-2','PUT',422,sha='0'*64);transfer('managed-2','PUT',200)
        post('managed-2','shot/ready',**args,size=len(blob));transfer('managed-3','GET',403);transfer('manual','GET',403)
        s=post('manual','shot/status',id='demo',ix=a['ix'],lease_token=j['lease_token']);assert s['sha']==digest
        assert transfer('manual','GET',200,j['lease_token'])==blob
        with server.db() as c:c.execute("update jobs set lease='next'");c.execute("update shot_relay set created=0");server.shot_relay.gc(c,server.DATA,int(time.time()))
        transfer('manual','GET',409,j['lease_token']);assert not (Path(tmp)/'shot-relay'/t).exists()
        print('PASS three devices, private MP4 relay across isolated identities, integrity, stale lease and cleanup')
    finally:http.shutdown();http.server_close()
if __name__=='__main__':run(Path(sys.argv[1]))
