"""Private completed shots, independent of a temporary worker lease."""
import hashlib,hmac,json,os,re,shutil,tempfile,threading,time
import media

SLOTS=threading.BoundedSemaphore(3)

def setup(c):
 c.execute('''CREATE TABLE IF NOT EXISTS shot_checkpoints(
 job TEXT,ix INTEGER,spec TEXT,sha TEXT,size INTEGER,created INTEGER,
 PRIMARY KEY(job,ix,spec))''')

def spec(job):
 fields=('prompt','shots','seconds','quality','precision','route','parent','total_seconds')
 data={k:job[k] for k in fields}
 return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def path(root,jid,ix,fingerprint):
 return root/'shot-checkpoints'/hashlib.sha256(jid.encode()).hexdigest()/(str(ix)+'-'+fingerprint+'.mp4')

def retain(c,root,job,ix,source,sha,size):
 fingerprint=spec(job)
 prior=c.execute('SELECT sha FROM shot_checkpoints WHERE job=? AND ix=? AND spec=?',(job['id'],ix,fingerprint)).fetchone()
 if prior and prior['sha']!=sha:raise ValueError('completed shot already preserved')
 target=path(root,job['id'],ix,fingerprint);target.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
 # Hard-link before relay cleanup so removing a transfer ticket cannot remove the shot.
 if target.exists():
  with target.open('rb') as existing:
   if target.stat().st_size!=size or hashlib.file_digest(existing,'sha256').hexdigest()!=sha:raise ValueError('checkpoint file conflicts')
 else:
  os.link(source,target)
  fd=os.open(target.parent,os.O_RDONLY)
  try:os.fsync(fd)
  finally:os.close(fd)
 c.execute('INSERT OR IGNORE INTO shot_checkpoints VALUES(?,?,?,?,?,?)',(job['id'],ix,fingerprint,sha,size,int(time.time())))


def serve(h,jid,ix,root,db,fail):
 hdr=h.headers;putting=h.command=='PUT'
 who,_=h.worker_auth({'worker_id':hdr.get('X-H3-Worker-ID'),'gpu':hdr.get('X-H3-GPU')})
 runtime=hdr.get('X-H3-Runtime','')
 if not re.fullmatch('[a-f0-9]{24}',runtime):fail(403,'缺少运行时标识')
 wid=who+'/'+runtime
 def check(c):
  row=c.execute("SELECT * FROM jobs WHERE id=? AND state='running' AND deadline>=?",(jid,int(time.time()))).fetchone()
  if not row:fail(409,'任务租约已失效')
  if not 0<=ix<len(json.loads(row['shots'])):fail(400,'无效镜号')
  primary=row['wid']==wid and hmac.compare_digest(row['lease'],hdr.get('X-H3-Lease',''))
  helper=c.execute("SELECT 1 FROM shot_tasks WHERE job_id=? AND epoch=? AND ix=? AND helper=? AND ticket=? AND state='taken' AND deadline>=?",(jid,row['lease'],ix,wid,hdr.get('X-H3-Shot-Ticket',''),int(time.time()))).fetchone()
  if not primary and not helper:fail(403,'无权访问此镜头')
  return row
 with db() as c:
  job=check(c);fingerprint=spec(job)
  saved=c.execute('SELECT sha,size FROM shot_checkpoints WHERE job=? AND ix=? AND spec=?',(jid,ix,fingerprint)).fetchone()
 target=path(root,jid,ix,fingerprint)
 if not putting:
  if not saved:fail(404,'尚无此镜头备份')
  if not target.is_file() or target.stat().st_size!=saved['size']:fail(500,'镜头备份缺失，停止重复渲染')
  with target.open('rb') as f:
   if hashlib.file_digest(f,'sha256').hexdigest()!=saved['sha']:fail(500,'镜头备份校验失败')
   f.seek(0);h.send_response(200);h.send_header('Content-Type','video/mp4');h.send_header('Cache-Control','no-store');h.send_header('Content-Length',str(saved['size']));h.send_header('X-Content-SHA256',saved['sha']);h.end_headers()
   if h.command!='HEAD':shutil.copyfileobj(f,h.wfile,262144)
  return
 n=hdr.get('Content-Length','');want=hdr.get('X-Content-SHA256','')
 if not n.isdigit() or not 0<int(n)<=media.MAX or hdr.get('Transfer-Encoding') or not re.fullmatch('[a-f0-9]{64}',want):fail(400,'镜头大小或摘要无效')
 n=int(n)
 if saved and saved['sha']!=want:fail(409,'已完成的镜头不可覆盖')
 folder=root/'shot-checkpoints';folder.mkdir(mode=0o700,exist_ok=True)
 if shutil.disk_usage(folder).free<3*1024**3+n:fail(507,'镜头备份空间不足')
 if not SLOTS.acquire(False):fail(429,'镜头备份繁忙')
 tmp=None
 try:
  h.connection.settimeout(45)
  with tempfile.NamedTemporaryFile('w+b',dir=folder,prefix='upload-',suffix='.part',delete=False) as f:
   tmp=f.name;left=n;digest=hashlib.sha256()
   while left:
    chunk=h.rfile.read(min(left,262144))
    if not chunk:fail(400,'镜头上传不完整')
    f.write(chunk);digest.update(chunk);left-=len(chunk)
   if digest.hexdigest()!=want or not media.video_file(f,n):fail(422,'镜头校验失败')
   f.flush();os.fsync(f.fileno())
  with db() as c:
   current=check(c)
   if spec(current)!=fingerprint:fail(409,'镜头计划已变化')
   try:retain(c,root,current,ix,tmp,want,n)
   except ValueError:fail(409,'已完成的镜头不可覆盖')
  h.send(200,{'ok':True,'sha256':want,'bytes':n})
 finally:
  if tmp:os.unlink(tmp)
  SLOTS.release()
