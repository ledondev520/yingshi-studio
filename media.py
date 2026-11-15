"""Owner-only MP4 copies. No public storage URLs; Drive remains the source of record."""
import contextlib, hashlib, os, re, shutil, subprocess, tempfile, threading, time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

MAX=512*1024*1024
SLOTS=threading.BoundedSemaphore(2)
def initialize(data):
 directory=data/'media';directory.mkdir(mode=0o700,exist_ok=True)
 (data/'previews').mkdir(mode=0o700,exist_ok=True)
 for tmp in [*directory.glob('put-*.part'),*(data/'previews').glob('*.part')]:
  if tmp.stat().st_mtime<time.time()-3600:tmp.unlink(missing_ok=True)
def driveid(r):
 m=re.fullmatch(r'https://drive\.google\.com/file/d/([A-Za-z0-9_-]{10,160})/view(?:\?usp=(?:sharing|drive_link))?',r['result'] or '')
 return m.group(1) if m and r['state']=='done' else None
def path(data,id):return data/'media'/(hashlib.sha256(id.encode()).hexdigest()+'.mp4')
def ready(data,r):return bool(r['media_sha'] and path(data,r['id']).is_file())
def preview_path(data,r):
 return data/'previews'/(hashlib.sha256(r['id'].encode()).hexdigest()+'-'+r['media_sha']+'-v1.mp4')
def preview_status(data,r):
 if ('precision' in r.keys() and r['precision']=='image-bf16') or not ready(data,r):return None
 p=preview_path(data,r)
 return 'ready' if p.is_file() else 'failed' if p.with_suffix('.failed').exists() else 'pending'

def make_preview(data,r):
 source=path(data,r['id']);dest=preview_path(data,r);tmp=None
 try:
  if shutil.disk_usage(data).free<3*1024**3+source.stat().st_size:raise OSError('Preview storage low')
  with tempfile.NamedTemporaryFile(dir=dest.parent,suffix='.part',delete=False) as f:tmp=Path(f.name)
  subprocess.run(['nice','-n','10','ffmpeg','-nostdin','-v','error','-y','-threads','2','-i',str(source),
   '-map','0:v:0','-map','0:a:0?','-map_metadata','-1','-filter_threads','1','-vf',
   "scale=w='if(gte(iw,ih),min(iw,1280),min(iw,720))':h='if(gte(iw,ih),min(ih,720),min(ih,1280))':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
   '-c:v','libx264','-preset','fast','-crf','26','-maxrate','2500k','-bufsize','5000k',
   '-threads','2','-pix_fmt','yuv420p','-force_key_frames','expr:gte(t,n_forced*2)',
   '-c:a','aac','-b:a','96k','-movflags','+faststart','-f','mp4',str(tmp)],
   check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=900)
  with tmp.open('rb') as f:
   if not video_file(f,tmp.stat().st_size):raise ValueError('Invalid preview')
  # Already-small originals should never become larger just for previewing.
  if tmp.stat().st_size>=source.stat().st_size:shutil.copyfile(source,tmp)
  with tmp.open('rb') as f:os.fsync(f.fileno())
  os.replace(tmp,dest)
  dest.with_suffix('.failed').unlink(missing_ok=True)
 except (OSError,ValueError,subprocess.SubprocessError) as e:
  dest.with_suffix('.failed').touch()
  print('preview failed job=%s type=%s'%(r['id'],type(e).__name__),flush=True)
 finally:
  if tmp:tmp.unlink(missing_ok=True)

def run_preview_once(data,db):
 with db() as c:
  rows=c.execute("SELECT id,media_sha,precision FROM jobs WHERE state='done' AND media_sha IS NOT NULL AND COALESCE(precision,'quantized')!='image-bf16' ORDER BY created DESC,rowid").fetchall()
 for r in rows:
  if not ready(data,r):continue
  dest=preview_path(data,r);failed=dest.with_suffix('.failed')
  if dest.is_file() or (failed.exists() and time.time()-failed.stat().st_mtime<300):continue
  make_preview(data,r)
  return True
 return False

def run_previews(data,db):
 while True:
  try:
   if run_preview_once(data,db):continue
  except Exception as e:print('preview queue failed:',type(e).__name__,flush=True)
  time.sleep(5)
def pending(c,data):
 return [{'id':r['id'],'seconds':r['seconds'],'drive_id':driveid(r)} for r in c.execute("SELECT id,seconds,result,state,media_sha FROM jobs WHERE state='done' ORDER BY created DESC") if driveid(r) and not ready(data,r)][:80]
def boxes(f,start,end):
 pos=start;count=0
 while pos+8<=end:
  count+=1
  if count>50000:raise ValueError()
  f.seek(pos);h=f.read(8);size=int.from_bytes(h[:4],'big');kind=h[4:8];off=8
  if size==1:size=int.from_bytes(f.read(8),'big');off=16
  if not size:size=end-pos
  if size<off or pos+size>end:raise ValueError()
  yield kind,pos+off,pos+size
  pos+=size
 if pos!=end:raise ValueError()
def video_file(f,n):
 try:
  top=list(boxes(f,0,n))
  if not top or top[0][0]!=b'ftyp' or not any(k==b'mdat' and z-a>0 for k,a,z in top):return False
  for k,a,z in top:
   if k!=b'moov':continue
   for k2,b,y in boxes(f,a,z):
    if k2!=b'trak':continue
    for k3,c,x in boxes(f,b,y):
     if k3!=b'mdia':continue
     for k4,d,w in boxes(f,c,x):
      if k4==b'hdlr' and w-d>=12:
       f.seek(d+8)
       if f.read(4)==b'vide':return True
 except (ValueError,OSError):pass
 return False

def put(h,jid,data,db,worker,fail):
 worker({'worker_id':h.headers.get('X-H3-Worker-ID'),'gpu':h.headers.get('X-H3-GPU')})
 ids=parse_qs(urlsplit(h.path).query).get('drive_id',[])
 if len(ids)!=1:fail(400,'缺少对应的 Drive 文件标识')
 if h.headers.get('Content-Type','').split(';')[0]!='video/mp4':fail(415,'仅接受 MP4 视频')
 if h.headers.get('Transfer-Encoding') or not h.headers.get('Content-Length','').isdigit():fail(411,'需要文件大小')
 n=int(h.headers['Content-Length'])
 if n<=0 or n>MAX:fail(413,'MP4 大小须在 512 MiB 内')
 want=h.headers.get('X-Content-SHA256','').lower()
 if want and not re.fullmatch(r'[0-9a-f]{64}',want):fail(400,'文件摘要格式有误')
 with db() as c:
  row=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
  if row is None or driveid(row)!=ids[0]:fail(409,'成片与任务记录不匹配')
 if not SLOTS.acquire(blocking=False):fail(429,'正在同步其他视频，请稍后重试')
 tmp=None
 try:
  if shutil.disk_usage(data).free<3*1024**3+n:fail(507,'预览存储不足，请使用 Drive 原片')
  left=n;calc=hashlib.sha256()
  with tempfile.NamedTemporaryFile('w+b',prefix='put-',suffix='.part',dir=data/'media',delete=False) as f:
   tmp=Path(f.name)
   h.connection.settimeout(90)
   while left:
    chunk=h.rfile.read(min(262144,left))
    if not chunk:fail(400,'上传不完整')
    calc.update(chunk);f.write(chunk);left-=len(chunk)
   got=calc.hexdigest()
   if want and got!=want:fail(422,'文件摘要校验失败')
   f.flush()
   if not video_file(f,n):fail(422,'未检测到有效 MP4 视频轨')
   os.fsync(f.fileno())
  with db() as c:
   row=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
   if not row or driveid(row)!=ids[0]:fail(409,'任务记录已改变')
   if ready(data,row):
    if row['media_sha']!=got:fail(409,'这个任务已经同步了不同内容，请保留 Drive 原件')
   else:
    os.replace(tmp,path(data,jid))
    c.execute('UPDATE jobs SET media_sha=? WHERE id=?',(got,jid))
  return h.send(200,{'ok':True,'bytes':n,'sha256':got})
 finally:
  if tmp:tmp.unlink(missing_ok=True)
  SLOTS.release()

def serve(h,jid,data,db,fail,watch_ok=False,preview=False,download=False):
 with db() as c:
  visitor=None if watch_ok else h.visitor(c)
  r=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
  if not r or (visitor and r['visitor']!=visitor) or not driveid(r) or not ready(data,r):fail(404,'此成片尚未同步站内预览')
  etag='"'+r['media_sha']+'"'
 target=path(data,jid)
 if preview:
  if preview_status(data,r)!='ready':fail(409,'预览版准备中，高清原片可下载')
  target=preview_path(data,r);etag='"'+r['media_sha']+'-preview-v1"'
  if h.headers.get('If-None-Match') in (etag,'*'):
   h.send_response(304);h.send_header('ETag',etag);h.send_header('Cache-Control','private, no-cache');h.end_headers()
   return
 with open(target,'rb') as f:
  size=os.fstat(f.fileno()).st_size;first,last=0,size-1;partial=False
  raw=h.headers.get('Range') if h.headers.get('If-Range',etag)==etag else None
  if raw:
   m=re.fullmatch(r'bytes=(\d{0,18})-(\d{0,18})',raw)
   if m and any(m.groups()):
    a,b=m.groups()
    if a:first=int(a);last=min(last,int(b)) if b else last
    elif b:first=max(0,size-int(b))
   if not m or not any(m.groups()) or first>last or first>=size:
    return h.send(416,{'error':'无效的字节区间'},hdr={'Content-Range':f'bytes */{size}','Accept-Ranges':'bytes'})
   partial=True
  h.send_response(206 if partial else 200)
  h.send_header('Content-Type','video/mp4');h.send_header('Accept-Ranges','bytes');h.send_header('Content-Length',str(last-first+1));h.send_header('ETag',etag)
  h.send_header('Cache-Control','private, no-cache' if preview else 'private, no-store');h.send_header('X-Content-Type-Options','nosniff');h.send_header('Cross-Origin-Resource-Policy','same-origin')
  if download:h.send_header('Content-Disposition','attachment; filename="yingshi-'+jid+'.mp4"')
  if partial:h.send_header('Content-Range',f'bytes {first}-{last}/{size}')
  h.end_headers()
  if h.command=='HEAD':return
  f.seek(first);left=last-first+1
  try:
   while left:
    chunk=f.read(min(left,262144))
    if not chunk:break
    h.wfile.write(chunk);left-=len(chunk)
  except (BrokenPipeError,ConnectionResetError):pass
