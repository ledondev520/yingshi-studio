"""Bounded provisioning slots. Queue consumers never wait on these API calls."""
import requests
import contextlib,fcntl,hashlib,json,os,re,secrets,sqlite3,subprocess,sys,tempfile,time,uuid
from pathlib import Path
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from colab_rest import PublicColab,Capacity,PROJECT,RuntimeFiles
from kernel_link import KernelLink
from colab_cli.client import Variant,Accelerator
from colab_cli.state import StateStore,SessionState
DIR=Path(__file__).resolve().parent
sys.path.insert(0,str(DIR.parent if (DIR.parent/'fleet.py').exists() else Path('/opt/h3-studio/current')))
import fleet,shot_relay,preparation
DATA=Path(os.environ.get('H3_DATA','/var/lib/h3-studio'));PRIVATE=Path.home()
DB=DATA/'studio.sqlite';TOKEN=PRIVATE/'.config/colab-cli/token.json'
CLI='/opt/h3-studio/autowake-venv/bin/colab'
IDLE=300;BOOT=900;LIMIT_RETRY=45
def save(path,v):
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(v));tmp.replace(path)
def claimed_endpoints():
    claimed=set()
    for p in PRIVATE.glob('wake*.json'):
        try:d=json.loads(p.read_text())
        except Exception:continue
        if d.get('endpoint'):claimed.add(d['endpoint'])
        if d.get('pending') and d.get('notebook'):
            try:claimed.add('runtimes/h3-'+uuid.UUID(str(d['notebook'])).hex[:24])
            except ValueError:pass
    return claimed
def evict_orphans(cl):
    try:items=cl.list_assignments()
    except Exception:return 0
    if not isinstance(items,(list,tuple)):return 0
    candidates=[getattr(x,'endpoint','') for x in items]
    orphans=[ep for ep in candidates if isinstance(ep,str) and re.fullmatch(r'runtimes/h3-[0-9a-f]{24}',ep) and ep not in claimed_endpoints()]
    if not orphans:return 0
    freed=0
    for ep in orphans:
        if ep in claimed_endpoints():continue
        try:
            cl.unassign(ep);freed+=1
            print('reclaimed orphan runtime',ep,flush=True)
        except Exception as e:
            print('orphan release skipped',ep,type(e).__name__,flush=True)
    if freed:
        with database() as c:
            c.execute('delete from pool_retry')
            c.execute("delete from pool_signal where stage in ('limit','denied')")
    return freed
@contextlib.contextmanager
def database():
    c=sqlite3.connect(DB,timeout=15,isolation_level=None);c.row_factory=sqlite3.Row
    try:
        c.execute('BEGIN IMMEDIATE');yield c;c.commit()
    except BaseException:c.rollback();raise
    finally:c.close()
def creds():
    s=AuthorizedSession(Credentials.from_authorized_user_file(str(TOKEN)))
    s.headers['X-Goog-User-Project']=PROJECT;return s
FOLDER_NAME='映室 H3 自动成片'
def _folder_alive(auth,fid):
    if not fid or not re.fullmatch(r'[\w-]{10,160}',str(fid)):return False
    r=auth.get('https://www.googleapis.com/drive/v3/files/'+fid,params={'fields':'id,trashed'},timeout=15)
    if r.status_code in (403,404):return False
    r.raise_for_status();return not r.json().get('trashed')
def ensure_folder(auth,slot):
    shared=PRIVATE/'drive-folder.json'
    with open(PRIVATE/'drive-folder.lock','a') as lk:
        fcntl.flock(lk,fcntl.LOCK_EX)
        cand=None
        if shared.is_file():
            try:cand=json.loads(shared.read_text()).get('folder')
            except Exception:cand=None
        for fid in ([cand] if cand else [])+([slot.cache.get('folder')] if slot.cache.get('folder') and slot.cache.get('folder')!=cand else []):
            if _folder_alive(auth,fid):
                if cand!=fid:save(shared,{'folder':fid})
                if slot.cache.get('folder')!=fid:slot.cache['folder']=fid;slot.persist()
                return fid
        q=f"name='{FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        sr=auth.get('https://www.googleapis.com/drive/v3/files',params={'q':q,'orderBy':'createdTime','pageSize':1,'fields':'files(id)'},timeout=25);sr.raise_for_status()
        files=sr.json().get('files') or []
        if files:fid=files[0]['id']
        else:
            cr=auth.post('https://www.googleapis.com/drive/v3/files',json={'name':FOLDER_NAME,'mimeType':'application/vnd.google-apps.folder'},timeout=25);cr.raise_for_status()
            fid=cr.json()['id']
        save(shared,{'folder':fid});slot.cache['folder']=fid;slot.persist();return fid
def ticket(auth,job,slot):
    folder=ensure_folder(auth,slot)
    image=slot.cache.get('mode')=='image-bf16'
    mime='image/png' if image else 'video/mp4'
    r=auth.post('https://www.googleapis.com/upload/drive/v3/files',params={'uploadType':'resumable','fields':'id'},json={'name':'H3-'+job+('.png' if image else '.mp4'),'parents':[folder]},headers={'X-Upload-Content-Type':mime},timeout=25)
    r.raise_for_status();return r.headers['Location']
def setup(c):
    c.execute('create table if not exists pool_boots(device text primary key,mode text,until integer)')
    c.execute('create table if not exists pool_retry(mode text primary key,until integer)')
def online(c,device,t):
    return bool(c.execute('select 1 from workers where instr(id,?||\'/\')=1 and seen>?',(device,t-fleet.FRESH)).fetchone())
def deficit(c,device,t):
    modes=fleet.need(c,t)
    for r in c.execute('select * from pool_boots where device!=? and until>?',(device,t)):
        if r['mode'] in modes and not online(c,r['device'],t):modes.remove(r['mode'])
    return modes
def working(c,device,t):
    prefix=device+'/'
    parents=[r[0] for r in c.execute("select id from jobs where state='running' and instr(wid,?)=1",(prefix,))]
    helping=bool(c.execute("""select 1 from shot_tasks s join jobs j on j.id=s.job_id and j.lease=s.epoch
       where j.state='running' and s.state='taken' and s.deadline>=? and instr(s.helper,?)=1""",(t,prefix)).fetchone())
    return parents,helping
def choose(c,device,t,owned_mode=None,needs=None):
    if needs is None:needs=deficit(c,device,t)
    if owned_mode:return owned_mode if owned_mode in needs else None
    return next((m for m in needs if not c.execute('select 1 from pool_retry where mode=? and until>?',(m,t)).fetchone()),None)
class Slot:
    def __init__(self,i):
        self.index=i;suffix='' if not i else '-'+str(i+1)
        self.name='h3-autowake'+suffix;self.device='h3-autowake-service'+suffix
        self.path=PRIVATE/('wake'+suffix+'.json');self.identity=PRIVATE/('worker'+suffix+'.json')
        self.cache=json.loads(self.path.read_text()) if self.path.exists() else {'notebook':str(uuid.uuid4())}
        self.cache.setdefault('mode','quantized');self.store=StateStore();self.link=None
        self.boot_mono=time.monotonic();self.last_sweep=0;self.persist()
    def persist(self):save(self.path,self.cache)
    def register(self,a):
        info=a.runtime_proxy_info;s=SessionState(name=self.name,endpoint=a.endpoint,token=info.token,url=info.url,accelerator=a.accelerator.value)
        old=self.store.get(self.name)
        if old and old.endpoint==a.endpoint:s=old
        s.token=info.token;s.url=info.url;s.token_expires_at=info.expires_at();self.store.add(s);return s
    def connected(self,endpoint):return bool(self.link and self.link.endpoint==endpoint and self.link.is_alive())
    def attach(self,s,kernels=None):
        if self.connected(s.endpoint):return
        if kernels is None:
            fc=RuntimeFiles(s);r=requests.get(fc.url+'/api/kernels',headers=fc.headers,timeout=(5,15));r.raise_for_status();kernels=r.json()
        kernel=next((k for k in kernels if k['id']==s.kernel_id),None)
        if kernel is None and len(kernels)==1:kernel=kernels[0]
        if kernel is None:raise RuntimeError('Cannot identify the worker kernel; preserving existing work')
        if self.link:self.link.close()
        s.kernel_id=kernel['id'];self.store.add(s)
        self.link=KernelLink(s,kernel['id']);print(self.name,'execution connection attached',flush=True)
    def initialize(self,s):
        if not self.identity.exists():save(self.identity,{'worker_id':self.device,'worker_key':secrets.token_urlsafe(32)})
        ident=json.loads(self.identity.read_text());v=hashlib.sha256(ident['worker_key'].encode()).hexdigest()
        with database() as c:c.execute('insert or ignore into pairs(verifier,device,gpu,code,expiry,approved) values(?,?,?,NULL,0,1)',(v,self.device,s.accelerator))
        fc=RuntimeFiles(s);fc.upload(str(self.identity),'h3managedidentity.json')
        fc.upload(str(DIR/'preparation_client.py'),'h3_preparation.py')
        fc.upload(str(DIR.parent/'colab-image-worker.py' if self.cache['mode']=='image-bf16' else DIR/('cold-bf16.py' if self.cache['mode']=='bf16' else 'cold-worker.py')),'h3managedworker.py')
        response=requests.get(fc.url+'/api/kernels',headers=fc.headers,timeout=(5,15));response.raise_for_status()
        kernels=response.json()
        if any(k.get('execution_state')=='busy' for k in kernels):self.attach(s,kernels);return
        with database() as c:preparation.accept(c,self.device,self.cache['mode'],dict(stage='environment',bytes_done=0,bytes_total=0,files_done=0,files_total=0,reused_bytes=0),int(time.time()),lambda *a:None)
        # Keep provisioning responsive while the actual worker occupies its kernel.
        with (PRIVATE/(self.name+'-execution.log')).open('ab') as log:
            child=subprocess.Popen([CLI,'--auth','adc','exec','-s',self.name,'-f',str(DIR/'launch.py'),'--timeout','86400'],stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL)
        try:code=child.wait(timeout=2)
        except subprocess.TimeoutExpired:return
        if code:raise RuntimeError('kernel launch failed')
    def clear(self):
        if self.link:self.link.close();self.link=None
        for k in ('endpoint','pending','sent','born','launched','last','mode','external','retry','crashes'):self.cache.pop(k,None)
        self.persist()
        if self.store.get(self.name):self.store.remove(self.name)
        with database() as c:
            fleet.requeue_device(c,self.device,int(time.time()))
            c.execute('delete from pool_boots where device=?',(self.device,))
            c.execute('delete from pool_signal where device=?',(self.device,))
            c.execute('delete from pool_preparation where device=?',(self.device,))
            c.execute('delete from worker_drain where device=?',(self.device,))
            c.execute("delete from workers where instr(id,?||'/')=1",(self.device,))
    def release(self,cl,endpoint,reason='idle'):
        # Only the exact endpoint returned for our persisted slot is eligible.
        assert endpoint and self.cache.get('endpoint')==endpoint
        if self.cache.get('external'):return  # User-connected runtimes remain under the user's lifecycle control.
        with database() as c:
            jobs,helps=working(c,self.device,time.time())
            if jobs or helps:return
            c.execute('insert or ignore into worker_drain values(?)',(self.device,))
        try:
            print(self.name,'release requested',reason,flush=True)
            cl.unassign(endpoint)
        except Exception:
            with database() as c:c.execute('delete from worker_drain where device=?',(self.device,))
            raise
        self.clear()
        with database() as c:
            c.execute('delete from pool_retry')
            c.execute("delete from pool_signal where stage in ('limit','denied')")
        print(self.name,'released runtime',reason,flush=True)
    def step(self):
        t=int(time.time());a=self.cache;owned=a.get('endpoint');mode=a.get('mode','quantized');wid=self.device
        with database() as c:
            if self.index==0:
                fleet.requeue_expired(c,t)
                shot_relay.gc(c,DATA,t)
            live=online(c,wid,t)
            mine,helps=working(c,wid,t)
            raw_needs=fleet.need(c,t)
            needs=deficit(c,wid,t)
            want=choose(c,wid,t,mode if owned or a.get('pending') else None,needs)
            incompatible=bool(owned and live and not mine and not helps and raw_needs and not fleet.device_compatible_with_pending(c,wid,t))
            if incompatible:
                want=None
            limited_any=bool(c.execute("select 1 from pool_signal where stage='limit' and until>?",(t,)).fetchone())
            other_demand=bool(needs) or limited_any or incompatible
            prep_failed=bool(c.execute("select 1 from pool_preparation where device=? and stage='failed'",(wid,)).fetchone())
            if not TOKEN.exists():return
            if live:
                c.execute('delete from pool_signal where device=?',(wid,))
                c.execute('delete from pool_boots where device=?',(wid,))
            elif not want:
                c.execute("delete from pool_signal where device=? and stage NOT IN ('denied','limit')",(wid,))
                c.execute('delete from pool_boots where device=?',(wid,))
            do_sweep=self.index==0 and time.monotonic()-self.boot_mono>=15 and t-self.last_sweep>=120
            if not owned and not a.get('pending') and not want:
                if do_sweep:
                    self.last_sweep=t;evict_orphans(PublicColab(creds()))
                return
            if not owned and not a.get('pending') and a.get('retry',0)>t:
                if do_sweep:
                    self.last_sweep=t;evict_orphans(PublicColab(creds()))
                return
            if not live and want:
                c.execute('insert or replace into pool_signal values(?,?,?,?)',(wid,want,'loading' if owned else 'request',t+600))
                c.execute('insert or replace into pool_boots values(?,?,?)',(wid,want,t+600))
            playback_win=45 if (not want and other_demand) else 180
            playback=c.execute("select 1 from jobs where instr(wid,?||'/')=1 and state='done' and media_sha is null and updated>? limit 1",(wid,t-playback_win)).fetchone()
        if do_sweep:
            self.last_sweep=t;evict_orphans(PublicColab(creds()))
        if live and a.get('crashes'):a.pop('crashes',None);self.persist()
        if mine or helps or playback:a['last']=t;self.persist()
        idle_ttl=0 if (not want and other_demand) else (45 if not live else IDLE)
        crashed_out=not live and prep_failed and a.get('crashes',0)>=1
        unsent=[j for j in mine if j not in a.get('sent',[])]
        if owned and live and self.connected(owned) and not unsent and (mine or helps or playback or want or t-a.get('last',t)<idle_ttl):return
        if owned and not live and want and not crashed_out and t-a.get('launched',0)<(45 if prep_failed else 150):return
        auth=creds();cl=PublicColab(auth)
        ep=owned or ('runtimes/h3-'+uuid.UUID(a['notebook']).hex[:24] if a.get('pending') else None)
        machine=None
        if ep:
            try:machine=cl.get(ep)
            except Exception as e:
                if getattr(getattr(e,'response',None),'status_code',None)!=404:raise
                if owned:
                    self.clear();print(self.name,'runtime missing (404); work requeued',flush=True);return
                if not want:
                    if t-a.get('pending',t)>600:self.clear()
                    return
                if t<a.get('retry',0):return
        if machine and not owned:a.pop('pending',None);a.update(endpoint=machine.endpoint,born=t,last=t,launched=0,crashes=0);owned=machine.endpoint;self.persist()
        if not machine:
            if not want:return
            mode=want;a.update(mode=mode,pending=t,retry=t+300);self.persist()
            models=(Accelerator.H100,Accelerator.A100) if mode in ('bf16','image-bf16') else (Accelerator.H100,Accelerator.A100,Accelerator.G4)
            highmem=(mode in ('bf16','image-bf16'))
            def _try_assign():
                lim=False
                for model in models:
                    try:return cl.assign(uuid.UUID(a['notebook']),Variant.GPU,model,highmem=highmem),lim
                    except Capacity as exc:
                        if str(exc)=='TOO_MANY_ACTIVE_RUNTIMES':raise Capacity('TOO_MANY_ACTIVE_RUNTIMES') from exc
                        if model==models[-1]:raise Capacity(str(exc)) from exc
                return None,lim
            limited=False
            try:
                try:
                    machine,limited=_try_assign()
                except Capacity as exc:
                    limited=str(exc)=='TOO_MANY_ACTIVE_RUNTIMES'
                    if limited and evict_orphans(cl)>0:
                        limited=False
                        machine,limited=_try_assign()
                    else:
                        raise
            except Capacity as exc:
                limited=limited or str(exc)=='TOO_MANY_ACTIVE_RUNTIMES'
                backoff=LIMIT_RETRY if limited else 300
                a.pop('pending',None);a.pop('retry',None);self.persist()
                with database() as c:
                    c.execute('delete from pool_boots where device=?',(wid,))
                    c.execute('insert or replace into pool_retry values(?,?)',(mode,int(time.time())+backoff))
                    c.execute('insert or replace into pool_signal values(?,?,?,?)',(wid,mode,'limit' if limited else 'denied',int(time.time())+300))
                print(self.name,mode,'capacity refused; existing workers continue',flush=True);return
            with database() as c:c.execute('insert or replace into pool_signal values(?,?,?,?)',(wid,mode,'loading',int(time.time())+600))
            print(self.name,mode,machine.accelerator.value,'runtime allocated',flush=True)
            owned=machine.endpoint;a.pop('pending',None);a.pop('retry',None);a.update(endpoint=owned,born=t,last=t,launched=0,crashes=0,sent=[]);self.persist()
        if not mine and not helps and not playback and ((not want and t-a.get('last',t)>=idle_ttl) or (not live and (crashed_out or t-a.get('born',t)>BOOT))):
            broken=not live and (crashed_out or t-a.get('born',t)>BOOT) and bool(want)
            reason='preempt' if (not want and other_demand) else ('worker-crashed' if crashed_out else ('startup-timeout' if not live and t-a.get('born',t)>BOOT else 'idle'))
            self.release(cl,owned,reason)
            if broken and not a.get('endpoint'):
                with database() as c:c.execute('insert or replace into pool_retry values(?,?)',(mode,t+(60 if crashed_out else 1800)))
            return
        if live and not self.connected(owned):self.attach(self.register(machine))
        if not live and (want or mine or helps) and time.time()-a.get('launched',0)>(45 if prep_failed else 150):
            try:
                if prep_failed:a['crashes']=a.get('crashes',0)+1
                self.initialize(self.register(machine))
                a['launched']=time.time();self.persist()
            except Exception:
                a['launched']=time.time()-105;self.persist();raise
        if unsent:
            s=self.register(machine)
            for job in unsent:
                if not re.fullmatch('[0-9a-f]{32}',job):continue
                url=ticket(auth,job,self)
                with tempfile.NamedTemporaryFile('w',dir=PRIVATE) as f:
                    json.dump({'url':url},f);f.flush();RuntimeFiles(s).upload(f.name,'h3upload-'+job+'.json')
                a['sent']=(a.get('sent',[])+[job])[-80:];self.persist()
def loop(i):
    slot=Slot(i)
    while True:
        try:slot.step()
        except Exception as e:print(slot.name,type(e).__name__,flush=True)
        time.sleep(4)
def main():
    import multiprocessing
    os.umask(0o077);lock=open(PRIVATE/'pool.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    with database() as c:setup(c)
    procs=[multiprocessing.Process(target=loop,args=(i,)) for i in range(fleet.MAX_CARDS)]
    for p in procs:p.start()
    try:
        while all(p.is_alive() for p in procs):time.sleep(5)
    finally:
        for p in procs:
            if p.is_alive():p.terminate()
        for p in procs:p.join()
    raise RuntimeError('slot exited')
if __name__=='__main__':main()
