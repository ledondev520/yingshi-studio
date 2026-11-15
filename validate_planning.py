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

def safety_and_recovery():
 import h3_guidance
 # 1. Safety block (promptFeedback.blockReason / finishReason SAFETY / model refusal) fails fast with 422 without wasting retries.
 for blocked_event in (
  {'promptFeedback':{'blockReason':'PROHIBITED_CONTENT'}},
  {'candidates':[{'finishReason':'SAFETY','content':{'parts':[]}}]},
  {'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':json.dumps(['抱歉，我无法为您生成涉及露骨脱衣舞内容的分镜。'],ensure_ascii=False)}]}}]},
 ):
  calls=[]
  wire=b'data: '+json.dumps(blocked_event).encode()+b'\n\n'
  with tempfile.TemporaryDirectory() as tmp:
   keys=Path(tmp);(keys/'vertex-api-key').write_text('test-only')
   def resp(req,**kw):calls.append(req);return io.BytesIO(wire)
   try:
    with patch('urllib.request.urlopen',side_effect=resp):
     director.plan({'prompt':'测试敏感提示词','seconds':15},keys,server.fail,lambda *_:None)
   except server.Problem as exc:
    assert exc.code==422 and '安全策略限制' in str(exc), (exc.code, str(exc))
   else:
    raise AssertionError('Expected BlockedError -> 422')
  assert len(calls)==1, f'Safety block should not waste retries, got {len(calls)} calls'

 # Hydrogen safety block also fast-fails in 1 call with 422.
 img_calls=[]
 with tempfile.TemporaryDirectory() as tmp:
  keys=Path(tmp);(keys/'vertex-api-key').write_text('test-only')
  def img_resp(req,**kw):img_calls.append(req);return io.BytesIO(b'data: {"promptFeedback":{"blockReason":"SAFETY"}}\n\n')
  try:
   with patch('urllib.request.urlopen',side_effect=img_resp):
    image_director.plan({'prompt':'测试敏感生图','aspect':'square'},keys,server.fail,lambda *_:None)
  except server.Problem as exc:
   assert exc.code==422 and '安全策略限制' in str(exc)
  else:
   raise AssertionError('Expected image BlockedError -> 422')
 assert len(img_calls)==1

 # 2. Tolerant h3_guidance.unpack handles [CUT] before seconds, 's'/'秒' suffix, omitted 4th BGM field, and copied section headers.
 s1=h3_guidance.unpack('8s|女孩推门进入病房|||integrated_multimodal_description: [Shot 1] A young woman opens the door.|||overall_soundscape: Quiet room tone and footsteps.',0,'story',True)
 assert s1['seconds']==8 and s1['prompt']=='女孩推门进入病房' and s1['h3_prompt'].endswith('non_diegetic_music: N/A')
 s2=h3_guidance.unpack('[CUT] 7秒|老人微笑醒来|||[Shot 2] An elderly patient wakes up and smiles.|||Soft breathing.|||non_diegetic_music: Gentle piano.',1,'story',True)
 assert s2['seconds']==7 and s2.get('cut') is True and s2['prompt']=='老人微笑醒来'

 # 3. Transient SSE stream error + MAX_TOKENS truncation + minor duration drift self-repair cleanly.
 valid_arr=['STORY','HD',
  '8s|第一镜中文|||A calm wide shot of a sunlit courtyard.|||Birds chirping softly.',
  '[CUT] 6秒|第二镜中文|||Close-up of tea pouring into a ceramic cup.|||Warm water pouring|||N/A']
 attempt_Payloads=[
  b'data: {"error":{"code":503,"status":"UNAVAILABLE"}}\n\n',
  b'data: '+json.dumps({'candidates':[{'finishReason':'MAX_TOKENS','content':{'parts':[{'text':'["story","hd","15|半截'}]}}]}).encode()+b'\n\n',
  b'data: '+json.dumps({'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':json.dumps(valid_arr,ensure_ascii=False)}]}}]}).encode()+b'\n\n',
 ]
 seen_bodies=[]
 with tempfile.TemporaryDirectory() as tmp:
  keys=Path(tmp);(keys/'vertex-api-key').write_text('test-only')
  def seq_resp(req,**kw):
   seen_bodies.append(json.loads(req.data))
   return io.BytesIO(attempt_Payloads[len(seen_bodies)-1])
  with patch('urllib.request.urlopen',side_effect=seq_resp),patch('director.time.sleep'):
   out=director.plan({'id':'job123','prompt':'清晨茶馆泡茶','seconds':15},keys,server.fail,lambda *_:None)
 assert len(seen_bodies)==3
 assert seen_bodies[2]['generationConfig']['maxOutputTokens']>seen_bodies[1]['generationConfig']['maxOutputTokens']
 assert out['route']=='story' and out['quality']=='hd' and sum(x['seconds'] for x in out['shots'])==15
 print('PASS Argon/Hydrogen safety fast-fail (422, 0 retries), stream retry, MAX_TOKENS expansion, tolerant unpack & duration reconciliation')

if __name__=='__main__':streaming();independent(False);independent(True);safety_and_recovery()
