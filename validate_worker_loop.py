"""A short network interruption must resume the same lease, then consume the next job."""
import ast
from pathlib import Path
from types import SimpleNamespace

class StopLoop(BaseException):pass
class ConnectionError(Exception):pass
class Timeout(Exception):pass
class HTTPError(Exception):
 def __init__(self,status):self.response=SimpleNamespace(status_code=status)

def check(error,terminal=False):
 tree=ast.parse(Path(__file__).with_name('colab-worker.py').read_text())
 funcs=[n for n in tree.body if getattr(n,'name','') in ('worker_loop','transient_error')]
 active=None;renders=[];claims=[];finished=[];failures=[];ticks=0;issued=0
 def rpc(route,data):
  nonlocal active,ticks,issued
  if route.endswith('/update'):
   failures.append(data);active=None;return {}
  assert route.endswith('/claim')
  ticks+=1
  if ticks>6:raise StopLoop()
  claims.append(data)
  if active:
   return {'job':active} if data.get('resume_lease')==active['lease_token'] else {'job':None}
  if issued==2:raise StopLoop()
  active={'id':'one' if issued==0 else 'two','lease_token':'first' if issued==0 else 'second'};issued+=1
  return {'job':active}
 def render(job):
  nonlocal active
  renders.append(job['id'])
  if len(renders)==1:raise error
  finished.append(job['id']);active=None
 env={'rpc':rpc,'render_job':render,'requests':SimpleNamespace(ConnectionError=ConnectionError,Timeout=Timeout,HTTPError=HTTPError),'time':SimpleNamespace(sleep=lambda _:None)}
 exec(compile(ast.Module(body=funcs,type_ignores=[]),'<worker_loop>','exec'),env)
 try:env['worker_loop']()
 except StopLoop:pass
 if terminal:
  assert finished==['two'] and len(failures)==1 and failures[0]['id']=='one',(renders,claims,failures)
  print('PASS terminal error fails only the broken job and consumes the next')
 else:
  assert finished==['one','two'],(renders,claims,failures)
  assert not failures,failures
  assert claims[1].get('resume_lease')=='first'
  print('PASS',type(error).__name__,'resumes lease and consumes next job')
if __name__=='__main__':
 for error in [Timeout(),ConnectionError(),HTTPError(503),HTTPError(429)]:check(error)
 check(HTTPError(400),terminal=True)
 check(RuntimeError('invalid rendering plan'),terminal=True)

def helper_reconnect():
 import tempfile,hashlib
 tree=ast.parse(Path(__file__).with_name('colab-worker.py').read_text())
 funcs=[n for n in tree.body if getattr(n,'name','') in ('worker_loop','transient_error','render_assist')]
 with tempfile.TemporaryDirectory() as tmp:
  claims=0;attempts=[];released=[];ready=[];jobs=[]
  def rpc(route,data):
   nonlocal claims
   if route.endswith('/claim'):
    claims+=1
    if claims==1:return {'assist':{'job':{'id':'help'},'ix':1,'ticket':'same-ticket','relay':False}}
    if claims==2:return {'job':{'id':'next','lease_token':'lease-next'}}
    raise StopLoop()
   if route.endswith('/release'):released.append(data)
   if route.endswith('/ready'):ready.append(data)
   return {}
  def render_piece(job,ix,path,piece,previous,pulse):
   attempts.append(job['_shot_ticket'])
   if len(attempts)==1:raise Timeout()
   piece.write_bytes(b'completed-test-shot')
  env=dict(ACTIVE_DIR=Path(tmp),hashlib=hashlib,rpc=rpc,render_piece=render_piece,render_job=lambda job:jobs.append(job['id']),requests=SimpleNamespace(ConnectionError=ConnectionError,Timeout=Timeout,HTTPError=HTTPError),time=SimpleNamespace(sleep=lambda _:None))
  exec(compile(ast.Module(body=funcs,type_ignores=[]),'<helper_loop>','exec'),env)
  try:env['worker_loop']()
  except StopLoop:pass
  assert attempts==['same-ticket','same-ticket'] and not released and len(ready)==1 and jobs==['next'],(attempts,released,ready,jobs)
 print('PASS helper retains its ticket after timeout, finishes, then takes the next job')
if __name__=='__main__':helper_reconnect()
