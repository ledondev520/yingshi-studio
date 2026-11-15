"""Private, lease-bound transfer for Colab VMs without a shared filesystem."""
import hashlib,hmac,os,re,shutil,tempfile,threading,time
import media, fleet, checkpoints
slots=threading.BoundedSemaphore(fleet.MAX_CARDS-1)
def setup(c):
    c.execute('create table if not exists shot_relay(ticket text primary key,created integer,size integer,sha text)')
def gc(c,root,t):
    folder=root/'shot-relay'
    for (ticket,) in c.execute("""select b.ticket from shot_relay b where b.created<? and not exists
        (select 1 from shot_tasks s join jobs j on j.id=s.job_id where s.ticket=b.ticket and j.state='running' and j.lease=s.epoch and s.state in ('ready','taken')) limit 128""",(t-600,)).fetchall():
        if re.fullmatch('[0-9a-f]{32}',ticket):(folder/ticket).unlink(missing_ok=True)
        c.execute('delete from shot_relay where ticket=?',(ticket,))
    if folder.exists():
        for p in folder.glob('upload-*.part'):
            try:
                if p.stat().st_mtime<t-3600:p.unlink(missing_ok=True)
            except FileNotFoundError:pass
def serve(h,ticket,root,db,fail):
    putting=h.command=='PUT';hdr=h.headers
    who,_=h.worker_auth({'worker_id':hdr.get('X-H3-Worker-ID'),'gpu':hdr.get('X-H3-GPU')})
    runtime=hdr.get('X-H3-Runtime','')
    if not re.fullmatch('[a-f0-9]{24}',runtime):fail(403,'缺少运行时标识')
    wid=who+'/'+runtime
    def check(c):
        r=c.execute("""select b.sha,b.size,s.state,s.helper,s.deadline,j.wid,j.lease from shot_relay b join shot_tasks s on s.ticket=b.ticket
          join jobs j on j.id=s.job_id and j.lease=s.epoch where b.ticket=? and j.state='running' and j.deadline>=?""",(ticket,int(time.time()))).fetchone()
        if not r:fail(409,'分镜任务已过期')
        if putting:
            if r['helper']!=wid or r['state']!='taken' or r['deadline']<time.time()-1:fail(403,'副卡租约失效')
        elif r['wid']!=wid or r['state']!='ready' or not hmac.compare_digest(r['lease'],hdr.get('X-H3-Lease','')):fail(403,'主卡租约失效')
        return r
    with db() as c:r=check(c)
    folder=root/'shot-relay';folder.mkdir(mode=0o700,exist_ok=True);path=folder/ticket
    if not putting:
        if not r['sha'] or not path.is_file():fail(404,'片段未就绪')
        with path.open('rb') as f:
            h.send_response(200);h.send_header('Cache-Control','no-store');h.send_header('Content-Type','video/mp4');h.send_header('Content-Length',str(r['size']));h.end_headers();shutil.copyfileobj(f,h.wfile,262144)
        return
    n=int(hdr.get('Content-Length') or 0);want=hdr.get('X-Content-SHA256','')
    if not 0<n<=media.MAX or hdr.get('Transfer-Encoding') or not re.fullmatch('[a-f0-9]{64}',want):fail(400,'片段大小或摘要无效')
    if shutil.disk_usage(folder).free<3*1024**3+n:fail(507,'片段中转空间不足')
    if not slots.acquire(False):fail(429,'传输忙')
    tmp=None
    try:
        h.connection.settimeout(45)
        with tempfile.NamedTemporaryFile('w+b',dir=folder,prefix='upload-',suffix='.part',delete=False) as f:
            tmp=f.name;left=n;digest=hashlib.sha256()
            while left:
                chunk=h.rfile.read(min(left,262144))
                if not chunk:fail(400,'片段不完整')
                digest.update(chunk);f.write(chunk);left-=len(chunk)
            if digest.hexdigest()!=want or not media.video_file(f,n):fail(422,'片段校验失败')
        with db() as c:
            r=check(c)
            if r['sha'] and r['sha']!=want:fail(409,'片段已提交')
            with open(tmp,'rb') as committed:os.fsync(committed.fileno())
            task=c.execute('SELECT job_id,ix FROM shot_tasks WHERE ticket=?',(ticket,)).fetchone()
            job=c.execute('SELECT * FROM jobs WHERE id=?',(task['job_id'],)).fetchone()
            try:checkpoints.retain(c,root,job,task['ix'],tmp,want,n)
            except ValueError:fail(409,'已完成的镜头不可覆盖')
            os.replace(tmp,path);tmp=None;c.execute('update shot_relay set sha=?,size=? where ticket=?',(want,n,ticket))
        return h.send(200,{'ok':True})
    finally:
        if tmp:os.unlink(tmp)
        slots.release()
