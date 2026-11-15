"""A completed shot survives lost runtimes, lease replacement and relay GC."""
import hashlib,json,tempfile,threading,time,sys,urllib.request,urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer
import server

def run(src):
 with tempfile.TemporaryDirectory() as tmp:
  server.DATA=server.KEYS=Path(tmp);server.setup();(Path(tmp)/'worker-key').write_text('private-test')
  http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);threading.Thread(target=http.serve_forever,daemon=True).start()
  url='http://127.0.0.1:'+str(http.server_port)+'/api/worker/'
  blob=src.read_bytes();sha=hashlib.sha256(blob).hexdigest()
  def req(w,path,data=None,method=None,lease='',ticket='',want=sha,status=200):
   headers={'Authorization':'Bearer private-test','X-H3-Worker-ID':w,'X-H3-GPU':'A100','X-H3-Runtime':'a'*24,'X-H3-Lease':lease,'X-H3-Shot-Ticket':ticket,'X-Content-SHA256':want,'Content-Type':'video/mp4' if isinstance(data,bytes) else 'application/json'}
   if isinstance(data,dict):data=json.dumps(dict(worker_id=w,runtime_id='a'*24,gpu='A100',shot_v1=True,relay_v1=True,checkpoint_v1=True,**data)).encode()
   try:r=urllib.request.urlopen(urllib.request.Request(url+path,data=data,headers=headers,method=method),timeout=5)
   except urllib.error.HTTPError as e:r=e
   body=r.read();assert r.code==status,(path,r.code,status,body[:160]);return body
  try:
   with server.db() as c:
    shots=[dict(seconds=5,prompt='scene',cut=i>0) for i in range(3)]
    c.execute("INSERT INTO jobs(id,state,created,quality,seconds,prompt,shots) VALUES('demo','queued',1,'standard',15,'test',?)",(json.dumps(shots),))
   main=json.loads(req('main','claim',{}))['job']
   helper=json.loads(req('helper','claim',{}))['assist'];ix=helper['ix'];ticket=helper['ticket']
   req('helper','media/shot/'+ticket,blob,'PUT')
   req('helper','shot/ready',dict(id='demo',ix=ix,ticket=ticket,size=len(blob)))
   # The old runtime disappears and a different runtime owns the next lease.
   with server.db() as c:
    c.execute("UPDATE jobs SET state='queued',lease=NULL,wid=NULL,deadline=NULL WHERE id='demo'")
    server.shot_relay.gc(c,server.DATA,int(time.time())+601)
   nextjob=json.loads(req('replacement','claim',{}))['job']
   assert nextjob and nextjob['lease_token']!=main['lease_token']
   route=f'media/checkpoint/demo/{ix}'
   got=req('replacement',route,lease=nextjob['lease_token']);assert got==blob
   req('stranger',route,lease=nextjob['lease_token'],status=403)
   req('main',route,lease=main['lease_token'],status=403)
   # Primary results must be durable too; unknown GETs cannot silently count as complete.
   route='media/checkpoint/demo/0'
   req('replacement',route,lease=nextjob['lease_token'],status=404)
   req('replacement',route,blob,'PUT',lease=nextjob['lease_token'],want='0'*64,status=422)
   req('replacement',route,blob,'PUT',lease=nextjob['lease_token'])
   req('replacement',route,blob,'PUT',lease=nextjob['lease_token'])
   assert req('replacement',route,lease=nextjob['lease_token'])==blob
   # Crash after the durable file is linked but before the SQLite transaction commits.
   with server.db() as c:c.execute("DELETE FROM shot_checkpoints WHERE job='demo' AND ix=0")
   assert req('replacement',route,lease=nextjob['lease_token'])==blob
   req('replacement','media/checkpoint/demo/99',lease=nextjob['lease_token'],status=400)
   # Changing a plan or quality cannot reuse an incompatible checkpoint.
   with server.db() as c:c.execute("UPDATE jobs SET quality='hd' WHERE id='demo'")
   req('replacement',route,lease=nextjob['lease_token'],status=404)
   print('PASS completed helper + primary survive replacement, immutable checksums, auth and plan invalidation')
  finally:http.shutdown();http.server_close()
if __name__=='__main__':run(Path(sys.argv[1]))

def restored_worker_skips_rendering(src):
 import ast
 tree=ast.parse(Path(__file__).with_name('colab-worker.py').read_text())
 funcs=[n for n in tree.body if getattr(n,'name','') in ('restore_pieces','render_piece','render_job')]
 class Assembly(Exception):pass
 with tempfile.TemporaryDirectory() as tmp:
  root=Path(tmp);calls=[];uploaded=[];blob=src.read_bytes()
  def checkpoint(job,ix,piece,upload=False):
   if upload:uploaded.append(ix);return True
   if ix<2:piece.write_bytes(blob);return True
   return False
  def render(job,ix,path,piece,previous,update):
   assert previous.read_bytes()==blob;calls.append(ix);piece.write_bytes(blob)
  def rpc(route,data):
   if data['progress']==92:raise Assembly()
  env=dict(ACTIVE_DIR=root,videos=root,hashlib=hashlib,json=json,rpc=rpc,dimensions=lambda j:(32,32),checkpoint_piece=checkpoint,_render_piece=render,durable=lambda p,d:p.write_text(json.dumps(d)))
  exec(compile(ast.Module(body=funcs,type_ignores=[]),'<worker>','exec'),env)
  try:env['render_job'](dict(id='demo',lease_token='new',seconds=15,checkpoint_spec='plan',shots=[dict(seconds=5,prompt='scene') for _ in range(3)]))
  except Assembly:pass
  assert calls==[2] and uploaded==[2],(calls,uploaded)
  print('PASS real render_job restores first two shots and renders only missing third')
if __name__=='__main__':restored_worker_skips_rendering(Path(sys.argv[1]))
