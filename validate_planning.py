"""Partial image text and independent image/video planning, with no paid calls."""
import contextlib,io,json,tempfile,threading,time
from pathlib import Path
from unittest.mock import patch
import director,image_director,server

def streaming():
 description='山间茶馆\n门牌写着“山间茶事”，窗内灯光温暖。'
 prompt='A wooden teahouse with a sign reading "山间茶事". '+ 'Soft dawn light and wet stone textures. '*10
 raw=json.dumps([description,prompt],ensure_ascii=True)
 chunks=[raw[:13],raw[13:48],raw[48:100],raw[100:180],raw[180:]]
 events=[{'candidates':[{'content':{'parts':[{'text':'private thought fixture','thought':True}]}}]}]+[{'candidates':[{'content':{'parts':[{'text':x}]}}]} for x in chunks]
 wire=b''.join(b'data: '+json.dumps(e).encode()+b'\n\n' for e in events)
 seen=[];requests=[]
 with tempfile.TemporaryDirectory() as tmp:
  keys=Path(tmp);(keys/'vertex-api-key').write_text('test-only')
  def response(req,**kw):requests.append(req);return io.BytesIO(wire)
  with patch('urllib.request.urlopen',side_effect=response):result=image_director.plan({'prompt':'山间茶馆','aspect':'square'},keys,server.fail,seen.append)
 assert result['model']=='gemini-4-hydrogen'
 assert result['shots'][0]['prompt']==description and result['shots'][0]['h3_prompt']==prompt.strip()
 assert len(seen)>=3 and seen[0][0]!=description and seen[-1]==[description,prompt],seen
 assert any(len(x)>1 and x[1]!=prompt for x in seen)
 assert all('private thought fixture' not in str(x) for x in seen)
 assert ':streamGenerateContent?alt=sse' in requests[0].full_url and '/gemini-4-hydrogen:' in requests[0].full_url
 assert json.loads(requests[0].data)['generationConfig']['thinkingConfig']=={'thinkingLevel':'LOW'}
 print('PASS Hydrogen LOW SSE, partial Chinese/English, escaped text, thought filtering and final validation')

def independent(slow_image):
 with tempfile.TemporaryDirectory() as tmp:
  server.DATA=server.KEYS=Path(tmp);server.setup()
  with server.db() as c:
   for image in (False,True):
    c.execute("INSERT INTO jobs(id,prompt,precision,seconds,state,shots,aspect,created) VALUES(?,?,?,?,'planning','[]','square',?)",('image' if image else 'video','test','image-bf16' if image else 'quantized',0 if image else 15,1 if image==slow_image else 2))
  release=threading.Event();slow_started=threading.Event();stop=threading.Event();calls=[];errors=[]
  original_db=server.db
  @contextlib.contextmanager
  def checked_db():
   if stop.is_set():raise SystemExit()
   with original_db() as c:yield c
  def plan(image,d,keys,fail,emit=None):
   assert (d['precision']=='image-bf16')==image;calls.append(d['id'])
   assert emit is not None
   emit(['画面正在写','English prompt in progress'] if image else ['视频分镜正在写'])
   if image==slow_image:slow_started.set();assert release.wait(4),'test release timed out'
   return {'model':'image-model' if image else 'video-model','route':'image' if image else 'story','quality':'standard','shots':[{'prompt':'finished','seconds':0 if image else 15}]}
  def thread(image):
   try:server.run_planner(image)
   except SystemExit:pass
   except BaseException as e:errors.append(e)
  threads=[threading.Thread(target=thread,args=(image,),daemon=True) for image in (False,True)]
  try:
   with patch.object(server,'db',checked_db),patch.object(image_director,'plan',side_effect=lambda *a,**kw:plan(True,*a,**kw)),patch.object(director,'plan',side_effect=lambda *a,**kw:plan(False,*a,**kw)):
    for t in threads:t.start()
    assert slow_started.wait(2),errors
    fast='video' if slow_image else 'image';slow='image' if slow_image else 'video'
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
     with original_db() as c:rows={r['id']:dict(r) for r in c.execute('SELECT * FROM jobs')}
     if rows[fast]['state']=='queued':break
     time.sleep(.01)
    assert rows[fast]['state']=='queued' and rows[slow]['state']=='planning',rows
    partial=json.loads(rows[slow]['shots']);assert partial and partial[0]['prompt']!='finished'
    if slow_image:assert partial[0]['h3_prompt']=='English prompt in progress'
    release.set()
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
     with original_db() as c:states=[r[0] for r in c.execute('SELECT state FROM jobs')]
     if states==['queued','queued']:break
     time.sleep(.01)
    assert states==['queued','queued'] and sorted(calls)==['image','video'] and not errors,(states,calls,errors)
    stop.set();server.plan_wake.set()
    for t in threads:t.join(3)
  finally:
   release.set();stop.set();server.plan_wake.set()
   for t in threads:t.join(3)
  assert not any(t.is_alive() for t in threads)
 print('PASS '+('image' if slow_image else 'video')+' planning cannot block the other queue; partial text survives before completion')

if __name__=='__main__':streaming();independent(False);independent(True)
