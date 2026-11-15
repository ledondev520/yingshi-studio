"""Two images reuse one pipeline; delivery outages and restart never resample saved PNGs."""
import contextlib,importlib.util,json,tempfile,types
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('image_worker',Path(__file__).with_name('colab-image-worker.py'))
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
class End(BaseException):pass
class NetworkError(Exception):response=None
class Response:
 def __init__(self,value):self.value=value
 def raise_for_status(self):pass
 def json(self):return self.value
class Image:
 def __init__(self,width,height):self.size=(width,height)
 def convert(self,mode):return self
 def save(self,path):path.write_bytes(b'PNG-test-fixture')

def run(manual=False):
 with tempfile.TemporaryDirectory() as tmp:
  root=Path(tmp);identity=root/'identity.json';identity.write_text(json.dumps({'worker_id':'test','worker_key':'test-only'}))
  signal=root/'upload.json';signal.write_text(json.dumps({'url':'https://www.googleapis.com/upload/test'}))
  counts={'loads':0,'renders':0,'drive':0,'preview':0};updates=[]
  jobs=[{'id':str(n)*32,'lease_token':'lease','shots':[{'h3_prompt':'test','width':2048,'height':2048}]} for n in (1,2)]
  def prepare():
   counts['loads']+=1
   def pipe(**kw):counts['renders']+=1;return [Image(kw['width'],kw['height'])]
   return pipe
  def post(url,json,**kw):
   if url.endswith('/claim'):
    if not jobs:raise End()
    return Response({'job':jobs.pop(0)})
   if url.endswith('/update'):updates.append(json)
   return Response({'ok':True})
  def put(url,**kw):
   name='drive' if 'googleapis.com' in url else 'preview';counts[name]+=1
   if counts[name]==1:raise NetworkError('transient upload outage')
   return Response({'id':'drive_file_123456'})
  fake_requests=types.SimpleNamespace(post=post,put=put,RequestException=NetworkError)
  fake_torch=types.SimpleNamespace(inference_mode=contextlib.nullcontext,Generator=lambda:types.SimpleNamespace(manual_seed=lambda seed:seed))
  fake_pil=types.SimpleNamespace(ImageStat=types.SimpleNamespace(Stat=lambda image:types.SimpleNamespace(stddev=[3])))
  class Thread:
   def __init__(self,**kw):pass
   def start(self):pass
  real_open=open
  def local_open(path,*a,**kw):return real_open(root/'worker.lock' if str(path)=='/content/h3-image-worker.lock' else path,*a,**kw)
  def local_path(path):
   if str(path) in ('/content/h3-image-results','/content/drive/MyDrive/MiniMax-H3-Image-BF16/outputs/studio'):return root/'results'
   if str(path)=='/content/drive/MyDrive/MiniMax-H3/studio-device.json':return identity
   return Path(path)
  with patch.dict('sys.modules',{'requests':fake_requests,'torch':fake_torch,'PIL':fake_pil}),patch.object(worker,'hardware',return_value=('NVIDIA A100-SXM4-80GB',81920)),patch.object(worker,'prepare',prepare),patch.object(worker,'exchange',side_effect=lambda name:(None if manual else identity) if name=='h3managedidentity.json' else signal),patch.object(worker,'Path',side_effect=local_path),patch('builtins.open',local_open),patch.object(worker.threading,'Thread',Thread),patch.object(worker.time,'sleep',return_value=None),patch.object(worker.os,'getxattr',create=True,side_effect=[b'',b'drive_file_123456',b'drive_file_234567']):
   try:worker.run()
   except End:pass
   assert counts['loads']==1 and counts['renders']==2,counts
   assert len(list((root/'results').glob('*.png')))==2
   assert len(list((root/'results').glob('*.synced')))==2
   assert all(x['state']=='done' for x in updates),updates
   # Simulate a crash after Drive commit but before private-preview acknowledgement.
   (root/'results'/('2'*32+'.synced')).unlink();old=counts['preview']
   try:worker.run()
   except End:pass
   assert counts['renders']==2 and counts['preview']==old+1,counts
  print('PASS one pipeline for two jobs, Drive/preview retries without regeneration, saved PNG recovery after restart')

if __name__=='__main__':run();run(manual=True)
