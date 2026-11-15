#!/usr/bin/env python3
import shutil
import threading
import media
import model_cache
import checkpoints
import director
import image_director
import sampling
import shot_tasks, fleet, shot_relay
import base64, contextlib, hashlib, hmac, json, os, re, secrets, sqlite3, time, uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get("STUDIO_DATA","/var/lib/h3-studio"))
KEYS=Path(os.environ.get("STUDIO_KEYS","/etc/h3-studio"))
ORIGIN=os.environ.get("STUDIO_ORIGIN","https://withkeya.com")
PREFIX="/h3-studio"
PORT=int(os.environ.get("STUDIO_PORT","18763"))
MAX_PENDING=320
MAX_VISITOR_PENDING=10
MAX_IP_SUBMISSIONS=600
def key(n):return (KEYS/n).read_text().strip()
def now():return int(time.time())
def pack(x):return json.dumps(x,ensure_ascii=False,separators=(",",":"))
def _watch_secret():
 p=DATA/"watch.key"
 if not p.is_file():
  DATA.mkdir(parents=True,exist_ok=True)
  tmp=p.with_suffix(".tmp");fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
  try:os.write(fd,secrets.token_bytes(32))
  finally:os.close(fd)
  tmp.replace(p)
 return p.read_bytes()
def watch_token(jid):return hmac.new(_watch_secret(),("watch:"+jid).encode(),hashlib.sha256).hexdigest()[:32]
def valid_watch(jid,tok):return isinstance(jid,str) and isinstance(tok,str) and bool(re.fullmatch(r"[a-f0-9]{32}",tok)) and hmac.compare_digest(watch_token(jid),tok)
@contextlib.contextmanager
def db():
 c=sqlite3.connect(DATA/"studio.sqlite",timeout=12,isolation_level=None);c.row_factory=sqlite3.Row
 try:
  c.execute("BEGIN IMMEDIATE");yield c;c.commit()
 except: c.rollback();raise
 finally:c.close()
def setup():
 DATA.mkdir(parents=True,exist_ok=True);media.initialize(DATA);_watch_secret()
 with sqlite3.connect(DATA/"studio.sqlite") as c:
  c.execute("PRAGMA journal_mode=WAL");checkpoints.setup(c)
  c.executescript("""CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, request TEXT UNIQUE, prompt TEXT, shots TEXT, seconds INTEGER, state TEXT, progress INTEGER, message TEXT, created INTEGER, updated INTEGER, wid TEXT, gpu TEXT, lease TEXT, deadline INTEGER, tries INTEGER DEFAULT 0, result TEXT);
  CREATE TABLE IF NOT EXISTS worker_modes(id TEXT PRIMARY KEY,precision TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS pairs(verifier TEXT PRIMARY KEY, device TEXT, gpu TEXT, code TEXT UNIQUE, expiry INTEGER, approved INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS workers(id TEXT PRIMARY KEY,gpu TEXT,seen INTEGER);
  CREATE TABLE IF NOT EXISTS drafts(id INTEGER PRIMARY KEY CHECK(id=1),data TEXT,updated INTEGER);
  CREATE TABLE IF NOT EXISTS activations(digest TEXT PRIMARY KEY,expiry INTEGER NOT NULL);
  CREATE TABLE IF NOT EXISTS sessions(digest TEXT PRIMARY KEY,expiry INTEGER);
  CREATE TABLE IF NOT EXISTS throttles(ip TEXT PRIMARY KEY,minute INTEGER,n INTEGER);
  CREATE INDEX IF NOT EXISTS job_queue ON jobs(state,created);
  """)
  c.execute(sampling.TABLE)
  c.execute(shot_tasks.TABLE)
  fleet.setup(c);shot_relay.setup(c)
  cols={r[1] for r in c.execute('PRAGMA table_info(jobs)')}
  if 'total_seconds' not in cols:c.execute('ALTER TABLE jobs ADD COLUMN total_seconds INTEGER')
  for k in ('visitor','parent','brief','media_sha','quality','route','director_model','audio_mode','precision','aspect'):
   if k not in cols:c.execute('ALTER TABLE jobs ADD COLUMN '+k+' TEXT')
class Problem(Exception):
 def __init__(self,code,msg):super().__init__(msg);self.code=code
def fail(code,msg):raise Problem(code,msg)
def s(a,n):return a.strip()[:n] if isinstance(a,str) else ''
def vidlink(u):return isinstance(u,str) and re.fullmatch(r'https://drive\.google\.com/file/d/[\w-]{10,160}/view(?:\?usp=(?:sharing|drive_link))?',u)
def precision(d):
 v=d.get('precision','quantized')
 if v not in ('quantized','bf16','image-bf16'):fail(400,'无效的生成模型')
 return v
def online_modes(c):
 return [r[0] for r in c.execute("SELECT DISTINCT COALESCE(m.precision,'quantized') FROM workers w LEFT JOIN worker_modes m ON m.id=w.id WHERE w.seen>?",(now()-100,))]
def jobrow(r,internal=False,c=None):
 x={k:r[k] for k in ('id','prompt','seconds','state','progress','message','created','updated','gpu')}
 x['precision']=r['precision'] or 'quantized'
 x['kind']='image' if x['precision']=='image-bf16' else 'video';x['aspect']=r['aspect'] or 'landscape'
 x['audio_mode']=r['audio_mode'] or 'original';x['quality']=r['quality'] or 'standard';x['shots']=json.loads(r['shots']);x['result_url']=r['result'] or '';x['parent']=r['parent'] or '';x['brief']=r['brief'] or r['prompt']
 x['media_ready']=media.ready(DATA,r) if r['state']=='done' else False
 x['preview_status']=media.preview_status(DATA,r) if x['media_ready'] else None
 x['total_seconds']=(r['total_seconds'] if 'total_seconds' in r.keys() else None) or r['seconds']
 x['append_source']=bool(x['parent'] and x['total_seconds']>x['seconds'])
 x['route']=r['route'];x['model']=r['director_model']
 if not internal:x['watch_token']=watch_token(r['id'])
 if not internal and c is not None:
  x['recovering']=(r['state']=='queued' and (r['tries'] or 0)>0) or (r['state']=='running' and now()-(r['updated'] or now())>fleet.FRESH)
  if r['state']=='running':
   x['sampling']=sampling.visible(c,r,now())
   if x['recovering']:x['message']='任务回报中断，正在检查 GPU；确认失联后自动重新调度'
 if internal:
  x['checkpoint_spec']=checkpoints.spec(r)
  # Existing Colab workers read shot.prompt; keep the owner's storyboard in Chinese.
  x['shots']=[{**shot,'prompt':shot.get('h3_prompt') or shot['prompt']} for shot in x['shots']]
  x['lease_token']=r['lease']
  if r['route'] in director.ROUTES:x['prompt']+='。镜头指导：'+director.ROUTES[r['route']]
 if c is not None and r['state']=='queued':
  t=now();x['queue_position']=fleet.queue_position(c,r,t)
  x['message']=fleet.wait_message(c,r['precision'] or 'quantized',t,r) or x['message']
  if x.get('recovering'):x['message']='GPU 断线，自动恢复中 · '+x['message']
 return x
def worker_status(wr,c=None):
 out={'online':bool(wr and now()-wr['seen']<100),'gpu':wr['gpu'] if wr else None,'seen':wr['seen'] if wr else None}
 if c is not None:out.update(fleet.cluster_summary(c,now()))
 return out
def cleanup(c):
 fleet.requeue_expired(c,now())
plan_wake=threading.Event()
def run_planner(images=False):
 while True:
  with db() as c:r=c.execute("SELECT id,prompt,seconds,precision,quality,audio_mode,aspect FROM jobs WHERE state='planning' AND (COALESCE(precision,'quantized')='image-bf16')=? ORDER BY created,rowid LIMIT 1",(images,)).fetchone()
  if not r:
   plan_wake.wait(2);plan_wake.clear();continue
  start=time.monotonic(); first=None; last=0
  def progress(parts):
   nonlocal first,last
   t=time.monotonic()
   if first is None:first=round(t-start,3)
   if t-last<.30:return
   last=t
   shots=[{'prompt':parts[0],'h3_prompt':parts[1] if len(parts)>1 else '','seconds':0}] if images else [{'prompt':x,'seconds':0} for x in parts]
   message='正在创作' if images else '正在写第 '+str(len(parts))+' 段分镜'
   with db() as c:c.execute("UPDATE jobs SET shots=?,updated=?,message=? WHERE id=? AND state='planning'",(pack(shots),now(),message,r['id']))
  try:
   result=image_director.plan(dict(r),KEYS,fail,progress) if images else director.plan(dict(r),KEYS,fail,progress)
   qual='hd' if r['precision']=='bf16' else (r['quality'] or result['quality'])
   with db() as c:c.execute("UPDATE jobs SET shots=?,quality=?,audio_mode=COALESCE(audio_mode,'auto'),route=?,director_model=?,state='queued',updated=?,message='创作描述已完成，等待 GPU' WHERE id=? AND state='planning'",(pack(result['shots']),qual,result['route'],result['model'],now(),r['id']))
   print('director model=%s first_s=%s total_s=%.3f shots=%d'%(result['model'],first,time.monotonic()-start,len(result['shots'])),flush=True)
  except Exception as e:
   code=e.code if isinstance(e,Problem) else 500
   msg=str(e) if isinstance(e,Problem) else '分镜生成失败，请重新提交。'
   with db() as c:c.execute("UPDATE jobs SET state='failed',shots='[]',message=?,updated=? WHERE id=? AND state='planning'",(msg,now(),r['id']))
   print('director failed job=%s kind=%s code=%d total_s=%.3f msg=%s'%(r['id'],'image' if images else 'video',code,time.monotonic()-start,msg),flush=True)

def worker_fields(d):
 wid=s(d.get('worker_id'),120);gpu=s(d.get('gpu'),120)
 if not re.fullmatch(r'[\w.:@-]{1,100}',wid):fail(400,'无效的工作端编号')
 if not re.search(r'\b(?:[AH]100|G4|RTX\s+PRO\s+6000\s+Blackwell)\b',gpu,re.I):fail(422,'此工作台仅接受 A100、H100 或 G4')
 return wid,gpu
def project_data(d):
 prompt=s(d.get('prompt'),4000);raw=d.get('shots');sec=d.get('seconds')
 if not prompt: fail(400,'请填写创意。')
 if type(sec) is not int or not (sec in (5,10) or 15<=sec<=120):fail(400,'片长支持 15 至 120 秒，以及已有的 5、10 秒试片。')
 if not isinstance(raw,list) or not 1<=len(raw)<=12:fail(400,'请使用一至十二个镜头。')
 shots=[]
 for item in raw:
  if not isinstance(item,dict) or not s(item.get('prompt'),2000) or type(item.get('seconds')) is not int or not 1<=item['seconds']<=15:fail(400,'每镜需要提示词，时长为 1 至 15 秒。')
  shots.append({'prompt':s(item['prompt'],2000),'seconds':item['seconds'],**({'cut':True} if shots and item.get('cut') is True else {})})
 if sum(i['seconds'] for i in shots)!=sec:fail(400,'各镜时长之和与成片不一致。')
 return prompt,shots,sec
def digest(tok):return hashlib.sha256(tok.encode()).hexdigest()
class Handler(BaseHTTPRequestHandler):
 server_version="H3Studio"
 def log_message(self,*args):pass
 def send(self,code,obj,ctype='application/json; charset=utf-8',hdr=None):
  blob=obj if isinstance(obj,bytes) else pack(obj).encode()
  self.send_response(code);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(blob)))
  for k,v in {'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; media-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",**(hdr or {})}.items():self.send_header(k,v)
  self.end_headers()
  if self.command!='HEAD': self.wfile.write(blob)
 def session(self,c):
  tok=next((a.strip()[7:] for a in self.headers.get('Cookie','').split(';') if a.strip().startswith('h3_sid=')),'')
  if not tok or not c.execute('SELECT 1 FROM sessions WHERE digest=? AND expiry>?',(digest(tok),now())).fetchone():fail(401,'请先解锁工作台')
  return digest(tok)
 def visitor(self,c):
  try:self.session(c);return None
  except Problem:pass
  tok=next((a.strip()[7:] for a in self.headers.get('Cookie','').split(';') if a.strip().startswith('h3_vid=')),'')
  if not re.fullmatch(r'[a-f0-9]{64}',tok):fail(401,'请刷新页面重新连接。')
  return digest(tok)
 def worker_auth(self,d):
  auth=self.headers.get('Authorization','')
  legacy=hmac.compare_digest(auth.encode(),('Bearer '+key('worker-key')).encode())
  if not legacy:
   if not auth.startswith('Bearer ') or not 16<=len(auth)<=300:fail(403,'工作端凭据错误')
   with db() as authdb:
    proof=authdb.execute('SELECT device FROM pairs WHERE approved=1 AND verifier=?',(digest(auth[7:]),)).fetchone()
   if not proof or proof['device']!=d.get('worker_id'):fail(403,'请配对此工作端')
  return worker_fields(d)
 def dispatch(self):
  parts=urlsplit(self.path);p=parts.path;qs=parse_qs(parts.query)
  qtoken=(qs.get('token') or [self.headers.get('X-H3-Watch-Token','')])[0]
  cache=re.fullmatch(r'/api/worker/model-cache/([0-9a-f]{64})',p)
  if cache and self.command=='GET':return model_cache.serve(self,cache[1],fail)
  m=re.fullmatch(r'/api/media/([A-Za-z0-9_-]{1,120})',p)
  if m and self.command in ('GET','HEAD'):return media.serve(self,m[1],DATA,db,fail,watch_ok=valid_watch(m[1],qtoken),download=qs.get('download')==['1'])
  m=re.fullmatch(r'/api/preview/([A-Za-z0-9_-]{1,120})',p)
  if m and self.command in ('GET','HEAD'):return media.serve(self,m[1],DATA,db,fail,watch_ok=valid_watch(m[1],qtoken),preview=True)
  checkpoint=re.fullmatch(r'/api/worker/media/checkpoint/([A-Za-z0-9_-]{1,120})/([0-9]{1,2})',p)
  if checkpoint and self.command in ('PUT','GET','HEAD'):return checkpoints.serve(self,checkpoint[1],int(checkpoint[2]),DATA,db,fail)
  relay=re.fullmatch(r'/api/worker/media/shot/([0-9a-f]{32})',p)
  if relay and self.command in ('PUT','GET'):return shot_relay.serve(self,relay[1],DATA,db,fail)
  m=re.fullmatch(r'/api/worker/media/([A-Za-z0-9_-]{1,120})',p)
  if m and self.command=='PUT':return media.put(self,m[1],DATA,db,self.worker_auth,fail)
  if self.command in ('GET','HEAD') and p in ('/colab-image-worker.py','/colab-worker.py','/colab-worker.ipynb','/colab-worker-bf16.ipynb'):
   return self.send(200,(ROOT/p[1:]).read_bytes(),'application/octet-stream',{'Content-Disposition':'attachment; filename="'+p[1:]+'"'})
  if self.command in ('GET','HEAD') and p in ('/','/index.html','/app.js','/style.css','/studio-art.svg'):
   name='index.html' if p in ('/','/index.html') else p[1:]
   return self.send(200,(ROOT/name).read_bytes(),{'html':'text/html; charset=utf-8','js':'application/javascript; charset=utf-8','css':'text/css; charset=utf-8','svg':'image/svg+xml'}[name.split('.')[-1]])
  if p=='/api/health' and self.command in ('GET','HEAD'):return self.send(200,{'ok':True,'service':'minimax-h3-studio'})
  if self.command=='POST':
   length=int(self.headers.get('Content-Length') or '0')
   if not 0<=length<=64000:fail(413,'内容过长')
   if self.headers.get('Content-Type','').split(';')[0]!='application/json':fail(415,'需要 JSON')
   d=json.loads(self.rfile.read(length) or b'{}')
   if not isinstance(d,dict):fail(400,'无效对象')
  else:d={}
  if p=='/api/pair/request' and self.command=='POST':
   wid,gpu=worker_fields({'worker_id':d.get('device_id'),'gpu':d.get('gpu')})
   ver=s(d.get('verifier'),128)
   if not re.fullmatch(r'[0-9a-f]{64}',ver):fail(400,'验证摘要有误')
   t=now();ip='pair:'+(self.headers.get('X-Real-IP','') or self.client_address[0]);bucket=t//600
   with db() as c:
    c.execute("DELETE FROM pairs WHERE approved=0 AND expiry<=?",(t,))
    old=c.execute("SELECT * FROM pairs WHERE verifier=?",(ver,)).fetchone()
    if old:
     if old['device']!=wid:fail(409,'设备不匹配')
     out={'approved':bool(old['approved']),'code':old['code'] if not old['approved'] else None,'expires_at':old['expiry']}
    else:
     r=c.execute('SELECT * FROM throttles WHERE ip=?',(ip,)).fetchone()
     if r and r['minute']==bucket and r['n']>=12:fail(429,'申请过于频繁')
     c.execute('INSERT INTO throttles VALUES(?,?,1) ON CONFLICT(ip) DO UPDATE SET minute=excluded.minute,n=CASE WHEN minute=excluded.minute THEN n+1 ELSE 1 END',(ip,bucket))
     code=secrets.token_hex(5).upper()
     c.execute('INSERT INTO pairs VALUES(?,?,?,?,?,0)',(ver,wid,gpu,code,t+600))
     out={'approved':False,'code':code,'expires_at':t+600}
   return self.send(200,out)
  if p.startswith('/api/worker/'):
   if self.command!='POST':fail(405,'不支持此操作')
   wid,gpu=self.worker_auth(d);mode=precision(d)
   if mode=='image-bf16' and (not re.search(r'\b[AH]100\b',gpu) or type(d.get('memory_mib')) is not int or d['memory_mib']<78000):fail(422,'生图仅接受 A100 80GB 或 H100 80GB')
   slot=d.get('runtime_id','')
   if slot and not re.fullmatch('[a-f0-9]{24}',str(slot)):fail(400,'无效显卡实例')
   if slot:wid=wid+'/'+slot
   if p=='/api/worker/sampler':
    with db() as c:sampling.accept(c,wid,d,now(),fail)
    return self.send(200,{'ok':True})
   if p=='/api/worker/source':
    with db() as c:
     child=c.execute("SELECT * FROM jobs WHERE id=? AND state='running' AND wid=?",(s(d.get('id'),100),wid)).fetchone()
     if not child or not hmac.compare_digest(child['lease'] or '!',s(d.get('lease_token'),128)) or not child['parent'] or not (child['total_seconds'] or 0)>child['seconds']:fail(403,'缺少当前续拍任务')
     source=media.path(DATA,child['parent'])
     if not source.is_file():fail(404,'原片未同步')
    with source.open('rb') as f:
     self.send_response(200);self.send_header('Content-Type','video/mp4');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(os.fstat(f.fileno()).st_size));self.end_headers();shutil.copyfileobj(f,self.wfile,262144)
    return
   with db() as c:
    cleanup(c);t=now();c.execute("INSERT INTO workers VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET gpu=excluded.gpu,seen=excluded.seen",(wid,gpu,t))
    # Older image workers reject 4:3; let active leases finish but give new jobs to upgraded workers.
    legacy_image=mode=='image-bf16' and d.get('image_43') is not True
    c.execute('INSERT INTO worker_modes VALUES(?,?) ON CONFLICT(id) DO UPDATE SET precision=excluded.precision',(wid,'image-bf16-legacy' if legacy_image else mode))
    fleet.register(c,wid,d)
    if p=='/api/worker/claim' and (legacy_image or c.execute('select 1 from worker_drain where device=?',(wid.split('/')[0],)).fetchone()):out={'job':None,'assist':None}
    elif p=='/api/worker/heartbeat':out={'ok':True}
    elif p=='/api/worker/media-pending':out={'jobs':media.pending(c,DATA)}
    elif p.startswith('/api/worker/shot/'):
     out=shot_tasks.handle(c,p.rsplit('/',1)[1],wid,d,t,fail)
    elif p=='/api/worker/claim':
     active=c.execute("SELECT * FROM jobs WHERE state='running' AND (wid=? OR wid=? OR (?='' AND substr(wid,1,length(?)+1)=? || '/')) ORDER BY created LIMIT 1",(wid,wid.split('/')[0],slot,wid,wid)).fetchone()
     r=None
     if active and (active['precision'] or 'quantized')==mode and active['wid']==wid and s(d.get('resume_lease'),128) and hmac.compare_digest(active['lease'],s(d.get('resume_lease'),128)):
      r=active
     occupied=bool(active or shot_tasks.busy(c,wid,t))
     assist=None
     if not occupied:
      r=c.execute("SELECT * FROM jobs WHERE state='queued' AND (total_seconds IS NULL OR total_seconds=seconds OR ?) AND COALESCE(precision,'quantized')=? AND (COALESCE(quality,'standard')='standard' OR (quality='fast' AND ?) OR (quality='hd' AND ?)) AND (COALESCE(audio_mode,'original')='original' OR ?) ORDER BY created,id LIMIT 1",(int(d.get('append_v1') is True),mode,int(d.get('turbo8') is True),int(d.get('finish_v2') is True),int(d.get('finish_v2') is True))).fetchone()
      if r:
       c.execute("UPDATE jobs SET state='running',message='GPU 已领取任务',progress=0,wid=?,gpu=?,lease=?,deadline=?,updated=?,tries=tries+1 WHERE id=?",(wid,gpu,secrets.token_urlsafe(24),t+600,t,r['id']))
       r=c.execute("SELECT * FROM jobs WHERE id=?",(r['id'],)).fetchone()
       if mode!='image-bf16' and slot and d.get('shot_v1') is True:shot_tasks.offer(c,r)
      elif mode!='image-bf16':assist=shot_tasks.claim(c,wid,d,t,jobrow)
     out={'job':jobrow(r,True) if r else None,'assist':assist}
     if r and c.execute('SELECT 1 FROM shot_tasks WHERE job_id=? AND epoch=? LIMIT 1',(r['id'],r['lease'])).fetchone():out['job']['parallel_cuts']=True
    elif p=='/api/worker/update':
     r=c.execute("SELECT * FROM jobs WHERE id=?",(s(d.get('id'),100),)).fetchone()
     if not r or (r['precision'] or 'quantized')!=mode or r['wid']!=wid or not r['lease'] or not hmac.compare_digest(r['lease'],s(d.get('lease_token'),128)):fail(409,'任务租约已失效')
     st=d.get('state');link=d.get('result_url') or None
     if st not in ('running','done','failed') or (st=='done' and not vidlink(link)):fail(400,'结果状态或云盘文件链接有误')
     if r['state']!='running':
      if r['state']==st and (st!='done' or r['result']==link):out={'ok':True}
      else:fail(409,'该任务已结束')
     else:
      pct=d.get('progress',0)
      if type(pct) not in (int,float) or not 0<=pct<=100:fail(400,'进度无效')
      c.execute("UPDATE jobs SET state=?,progress=?,message=?,result=?,deadline=?,updated=? WHERE id=?",(st,100 if st=='done' else int(pct),s(d.get('message'),360),link if st=='done' else None,t+600,t,r['id']));out={'ok':True}
    else:fail(404,'无此接口')
   return self.send(200,out)
  if self.command=='POST' and self.headers.get('Origin')!=ORIGIN:fail(403,'来源不匹配')
  if p=='/api/director' and self.command=='POST':
   with db() as c:self.session(c)
   if self.headers.get('Accept')!='application/x-ndjson':return self.send(200,director.plan(d,KEYS,fail))
   start=time.monotonic();first=None;started=False
   def event(obj):
    nonlocal started
    if not started:
     self.send_response(200);self.send_header('Content-Type','application/x-ndjson; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('X-Accel-Buffering','no');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Connection','close');self.end_headers();started=True;self.close_connection=True
    self.wfile.write((pack(obj)+'\n').encode());self.wfile.flush()
   def preview(parts):
    nonlocal first
    if first is None:first=round(time.monotonic()-start,3)
    event({'type':'preview','parts':parts})
   try:
    result=director.plan(d,KEYS,fail,preview)
    elapsed=round(time.monotonic()-start,3)
    event({'type':'done',**result})
    print('director model=%s first_s=%s total_s=%s shots=%d'%(result['model'],first,elapsed,len(result['shots'])),flush=True)
   except (ConnectionResetError,BrokenPipeError):pass
   except Exception as e:
    if not started:raise
    event({'type':'error','error':str(e) if isinstance(e,Problem) else '分镜输出中断，请重试。'})
   return
  cookie_attr=f'; Path={PREFIX}/; HttpOnly; SameSite=Strict'+('; Secure' if ORIGIN.startswith('https:') else '')
  if p=='/api/state' and self.command=='GET':
   with db() as c:
    try: self.visitor(c)
    except Problem:
     tok=secrets.token_hex(32);wr=c.execute('SELECT gpu,seen FROM workers ORDER BY seen DESC LIMIT 1').fetchone()
     return self.send(200,{'role':'guest','jobs':[],'draft':None,'worker':worker_status(wr,c),'online_modes':online_modes(c),'now':now()},hdr={'Set-Cookie':f'h3_vid={tok}; Max-Age=15552000'+cookie_attr})
  if p=='/api/activate' and self.command=='POST':
   raw=s(d.get('token'),180)
   with db() as c:
    c.execute('DELETE FROM activations WHERE expiry<=?',(now(),))
    if not c.execute('DELETE FROM activations WHERE digest=? AND expiry>?',(digest(raw),now())).rowcount:fail(410,'激活链接已用过或已过期，请在已连接设备上创建新链接')
    tok=secrets.token_urlsafe(36);c.execute('DELETE FROM sessions WHERE expiry<?',(now(),));c.execute('INSERT INTO sessions VALUES(?,?)',(digest(tok),now()+180*86400))
   return self.send(200,{'ok':True},hdr={'Set-Cookie':f'h3_sid={tok}; Max-Age=15552000'+cookie_attr})
  if p=='/api/login' and self.command=='POST':
   ip=self.headers.get('X-Real-IP','') or self.client_address[0];bucket=now()//600
   with db() as c:
    row=c.execute('SELECT * FROM throttles WHERE ip=?',(ip,)).fetchone()
    if row and row['minute']==bucket and row['n']>=10:fail(429,'尝试过多，请稍后再试')
    ok=hmac.compare_digest(s(d.get('password'),256).encode(),key('admin-key').encode())
    if not ok:c.execute('INSERT INTO throttles VALUES(?,?,1) ON CONFLICT(ip) DO UPDATE SET minute=excluded.minute,n=CASE WHEN minute=excluded.minute THEN n+1 ELSE 1 END',(ip,bucket))
    else:
     tok=secrets.token_urlsafe(36);c.execute('DELETE FROM sessions WHERE expiry<?',(now(),));c.execute('INSERT INTO sessions VALUES(?,?)',(digest(tok),now()+30*86400))
   if not ok:fail(401,'口令不正确')
   return self.send(200,{'ok':True},hdr={'Set-Cookie':f'h3_sid={tok}; Max-Age=2592000'+cookie_attr})
  extra_hdr=None
  with db() as c:
   mc=re.fullmatch(r'/api/creation/([a-f0-9]{32})',p)
   watch_creation=bool(mc and self.command=='GET' and valid_watch(mc[1],qtoken))
   watch_cancel=bool(p=='/api/cancel' and self.command=='POST' and valid_watch(s(d.get('id'),100),s(d.get('watch_token'),64) or qtoken))
   if watch_creation or watch_cancel:
    visitor=None
   else:
    try:visitor=self.visitor(c)
    except Problem:
     if p=='/api/create' and self.command=='POST':
      new_tok=secrets.token_hex(32);visitor=digest(new_tok);extra_hdr={'Set-Cookie':f'h3_vid={new_tok}; Max-Age=15552000'+cookie_attr}
     else:raise
   public=p in ('/api/state','/api/create','/api/cancel') or bool(mc)
   if visitor and not public:fail(403,'无权执行此操作')
   cleanup(c)
   if p=='/api/state' and self.command=='GET':
    dr=c.execute('SELECT data FROM drafts WHERE id=1').fetchone();wr=c.execute('SELECT gpu,seen FROM workers ORDER BY seen DESC LIMIT 1').fetchone()
    rows=c.execute('SELECT * FROM jobs WHERE ? IS NULL OR visitor=? ORDER BY created DESC,rowid DESC LIMIT 80',(visitor,visitor))
    out={'role':'guest' if visitor else 'owner','jobs':[jobrow(r,c=c) for r in rows],'draft':json.loads(dr['data']) if dr and not visitor else None,'worker':worker_status(wr,c),'online_modes':online_modes(c),'now':now()}
   elif p=='/api/devices/link' and self.command=='POST':
    token=secrets.token_urlsafe(36);exp=now()+600
    c.execute('DELETE FROM activations WHERE expiry<=?',(now(),));c.execute('INSERT INTO activations VALUES(?,?)',(digest(token),exp))
    out={'url':ORIGIN+PREFIX+'/#activate='+token,'expires_at':exp}
   elif p=='/api/pair/approve' and self.command=='POST':
    code=s(d.get('code'),20).replace('-','').upper()
    r=c.execute('SELECT device,gpu,verifier FROM pairs WHERE code=? AND expiry>? AND approved=0',(code,now())).fetchone()
    if not r:fail(400,'配对码无效或已过期')
    c.execute('UPDATE pairs SET approved=1,code=NULL WHERE verifier=?',(r['verifier'],))
    out={'ok':True,'device_id':r['device'],'gpu':r['gpu']}
   elif p=='/api/draft' and self.command=='POST':
    if not isinstance(d.get('shots',[]),list):fail(400,'分镜有误')
    draft={'kind':'image' if d.get('kind')=='image' else 'video','aspect':d.get('aspect') if d.get('aspect') in image_director.SIZES else 'landscape','precision':precision(d),'audio_mode':d.get('audio_mode') if d.get('audio_mode') in ('auto','original','continuous') else 'auto','quality':d.get('quality') if d.get('quality') in ('fast','hd','standard') else 'hd','parent':s(d.get('parent'),100),'route':d.get('route') if d.get('route') in ('continuous','story','dialogue','mood') else 'story','prompt':s(d.get('prompt'),4000),'seconds':d.get('seconds') if type(d.get('seconds')) is int and 5<=d['seconds']<=120 else 10,'shots':[{'prompt':s(i.get('prompt'),2000),'seconds':i.get('seconds') if i.get('seconds') in (5,10,15) else 10} for i in d.get('shots',[])[:12] if isinstance(i,dict)]}
    c.execute('INSERT INTO drafts VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,updated=excluded.updated',(pack(draft),now()));out={'ok':True}
   elif mc and self.command=='GET':
    r=c.execute('SELECT * FROM jobs WHERE id=?',(mc[1],)).fetchone()
    if not r or (not watch_creation and visitor and r['visitor']!=visitor):fail(404,'没有此任务')
    out={'job':jobrow(r,c=c)}
   elif p=='/api/create' and self.command=='POST':
    kind=d.get('kind','video');aspect=d.get('aspect','landscape')
    if kind not in ('video','image') or aspect not in image_director.SIZES:fail(400,'无效的创作类型或画幅')
    mode='image-bf16' if kind=='image' else precision(d)
    if kind=='video' and mode=='image-bf16':fail(400,'请选择图片创作')
    brief=s(d.get('prompt'),4000);sec=0 if kind=='image' else d.get('seconds');req=s(d.get('request_id'),100);parent=s(d.get('parent'),100) or None
    if kind=='image' and parent:fail(400,'图片不支持视频续拍')
    raw_am='original' if kind=='image' else d.get('audio_mode');raw_q='standard' if kind=='image' else d.get('quality')
    if raw_am is not None and raw_am not in ('auto','original','continuous'):fail(400,'无效的声音设置')
    if raw_q is not None and raw_q not in ('auto','standard','fast','hd'):fail(400,'无效生成模式')
    explicit_q=None if raw_q in (None,'auto') else raw_q
    if mode=='bf16' and explicit_q not in (None,'hd'):fail(400,'BF16 使用原始 768p 配置')
    if not brief or type(sec) is not int or not (sec==0 if kind=='image' else 15<=sec<=120) or not re.fullmatch(r'[a-zA-Z0-9-]{16,100}',req):fail(400,'请输入创意，并选择 15 至 120 秒。')
    r=c.execute('SELECT * FROM jobs WHERE request=?',(req,)).fetchone()
    if r and visitor and r['visitor']!=visitor:fail(409,'请求编号不可用。')
    if r and ((r['precision'] or 'quantized')!=mode or r['brief']!=brief or r['seconds']!=sec or r['parent']!=parent or (kind=='image' and r['aspect']!=aspect) or (raw_am is not None and (r['audio_mode'] or 'auto')!=raw_am) or (explicit_q is not None and r['state']=='planning' and r['quality']!=explicit_q)):fail(409,'同一请求编号的内容不同')
    if not r:
     if c.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('planning','queued','running')").fetchone()[0]>=MAX_PENDING:fail(429,'队列已满，请稍后再试')
     if visitor:
      ip='public:'+digest(self.headers.get('X-Real-IP') or self.client_address[0]);bucket=now()//3600
      limit=c.execute('SELECT minute,n FROM throttles WHERE ip=?',(ip,)).fetchone()
      if limit and limit['minute']==bucket and limit['n']>=MAX_IP_SUBMISSIONS:fail(429,'本小时提交较多，请稍后再试。')
      if c.execute("SELECT COUNT(*) FROM jobs WHERE visitor=? AND state IN ('planning','queued','running')",(visitor,)).fetchone()[0]>=MAX_VISITOR_PENDING:fail(429,f'已有 {MAX_VISITOR_PENDING} 个任务等待或生成，请等任务完成后再提交。')
      c.execute('INSERT INTO throttles VALUES(?,?,1) ON CONFLICT(ip) DO UPDATE SET minute=excluded.minute,n=CASE WHEN minute=excluded.minute THEN n+1 ELSE 1 END',(ip,bucket))
     prompt=brief;total=sec
     if parent:
      prior=c.execute('SELECT * FROM jobs WHERE id=? AND (? IS NULL OR visitor=?)',(parent,visitor,visitor)).fetchone()
      if not prior:fail(404,'上一版记录不存在')
      if prior['precision']=='image-bf16':fail(400,'图片不能作为续拍原片')
      if prior['state']!='done' or not media.ready(DATA,prior):fail(409,'原片尚未就绪，请在成片同步后续拍。')
      total=(prior['total_seconds'] or prior['seconds'])+sec
      prompt='仅编排原片之后新增的 '+str(sec)+' 秒。第一镜从原片最后一帧继续，保持角色、机位和画幅。原片背景：'+prior['prompt'][-3000:]+'。接下来发生：'+brief
     jid=uuid.uuid4().hex
     c.execute("INSERT INTO jobs(precision,visitor,id,request,parent,brief,prompt,seconds,state,progress,shots,message,created,updated) VALUES(?,?,?,?,?,?,?,?,'planning',0,'[]','已收到创意，等待编排',?,?)",(mode,visitor,jid,req,parent,brief,prompt,sec,now(),now()))
     c.execute('UPDATE jobs SET total_seconds=?,audio_mode=?,quality=?,aspect=? WHERE id=?',(total,raw_am,explicit_q,aspect,jid))
     r=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
    out={'job':jobrow(r,c=c)};plan_wake.set()
   elif p=='/api/jobs' and self.command=='POST':
    mode=precision(d)
    if mode=='image-bf16':fail(400,'图片请通过创作入口提交')
    quality=d.get('quality','hd' if mode=='bf16' else 'standard')
    if mode=='bf16' and quality!='hd':fail(400,'BF16 使用原始 768p 配置')
    audio_mode=d.get('audio_mode','original')
    if audio_mode not in ('original','auto','continuous'):fail(400,'无效的声音设置')
    if quality not in ('standard','fast','hd'):fail(400,'无效生成模式')
    prompt,shots,sec=project_data(d);req=s(d.get('request_id'),100)
    if not re.fullmatch(r'[a-zA-Z0-9-]{16,100}',req):fail(400,'请求编号无效')
    parent=s(d.get('parent'),100) or None; brief=prompt
    if parent:
     source=c.execute('SELECT prompt FROM jobs WHERE id=?',(parent,)).fetchone()
     if source is None:fail(404,'上一版记录不存在')
     prompt='上一版创意：'+source['prompt'][-3600:]+'。本次修改，以本轮要求为准：'+brief
    r=c.execute('SELECT * FROM jobs WHERE request=?',(req,)).fetchone()
    if r and ((r['precision'] or 'quantized')!=mode or (r['audio_mode'] or 'original')!=audio_mode or (r['quality'] or 'standard')!=quality or r['parent']!=parent or r['prompt']!=prompt or json.loads(r['shots'])!=shots or r['seconds']!=sec):fail(409,'同一请求编号的内容不同')
    if not r:
     if c.execute("SELECT count(*) FROM jobs WHERE state IN ('planning','queued','running')").fetchone()[0]>=MAX_PENDING:fail(429,'队列已满，请稍后再试')
     jid=uuid.uuid4().hex
     c.execute("INSERT INTO jobs(id,request,prompt,shots,seconds,state,progress,message,created,updated) VALUES(?,?,?,?,?,'queued',0,'已保存，等待 GPU',?,?)",(jid,req,prompt,pack(shots),sec,now(),now()));c.execute('UPDATE jobs SET parent=?,brief=?,quality=?,audio_mode=?,precision=? WHERE id=?',(parent,brief,quality,audio_mode,mode,jid));r=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
    out={'job':jobrow(r,c=c)}
   elif p=='/api/cancel' and self.command=='POST':
    if not c.execute("UPDATE jobs SET state='cancelled',message='已取消',updated=? WHERE id=? AND state='queued' AND (? IS NULL OR visitor=?)",(now(),s(d.get('id'),100),visitor,visitor)).rowcount:fail(409,'仅可取消仍在队列的任务')
    out={'ok':True}
   elif p=='/api/logout' and self.command=='POST':
    c.execute('DELETE FROM sessions WHERE digest=?',(self.session(c),));out={'ok':True}
   else:fail(404,'无此接口')
  self.send(200,out,hdr=extra_hdr)
 def handle_req(self):
  try:self.dispatch()
  except Problem as e:self.send(e.code,{'error':str(e)})
  except (ValueError,TypeError,UnicodeError):self.send(400,{'error':'请求内容有误'})
  except (ConnectionResetError,BrokenPipeError):pass
  except Exception as e:
   print('request failed:',type(e).__name__,flush=True);self.send(500,{'error':'暂时无法处理'})
 do_GET=do_POST=do_HEAD=do_PUT=handle_req
if __name__=='__main__':
 import signal
 setup()
 for images in (False,True):threading.Thread(target=run_planner,args=(images,),daemon=True).start()
 threading.Thread(target=media.run_previews,args=(DATA,db),daemon=True).start()
 srv=ThreadingHTTPServer(('127.0.0.1',PORT),Handler)
 for sig in (signal.SIGTERM,signal.SIGINT):
  signal.signal(sig,lambda *_:threading.Thread(target=srv.shutdown,daemon=True).start())
 srv.serve_forever();srv.server_close()
