"""Compute the shortage, not just the number of idle GPUs. Shared by API and provisioner."""
import json
import checkpoints
MAX_CARDS=6
FRESH=100
def setup(c):
    c.execute('create table if not exists worker_caps(id text primary key,shots int,relay int,hd int,fast int,append_ok int)')
    c.execute('create table if not exists pool_signal(device text primary key,mode text,stage text,until integer)')
    c.execute('create table if not exists worker_drain(device text primary key)')
def register(c,wid,d):
    c.execute('insert or replace into worker_caps values(?,?,?,?,?,?)',(wid,*(int(d.get(k) is True) for k in ('shot_v1','relay_v1','finish_v2','turbo8','append_v1'))))
def cuts(shots,route):
    return [i for i,s in enumerate(shots) if i and s.get('cut') is True] if len(shots)>=3 and route!='continuous' else []
def device(wid):return wid.partition('/')[0]
def _live_workers(c,t):
    return {r['id']:r for r in c.execute("""SELECT w.id,coalesce(m.precision,'quantized') mode,k.shots,k.relay,k.hd,k.fast,k.append_ok
        FROM workers w LEFT JOIN worker_modes m ON m.id=w.id LEFT JOIN worker_caps k ON k.id=w.id
        WHERE w.seen>? AND NOT EXISTS(SELECT 1 FROM worker_drain d WHERE w.id=d.device OR instr(w.id,d.device||'/')=1)""",(t-FRESH,))}
def _compatible(wr,j):
    mode=j['precision'] or 'quantized'
    q=j['quality'] or 'standard'
    am=j['audio_mode'] or 'original'
    tot=j['total_seconds'] if 'total_seconds' in j.keys() else None
    return (wr['mode']==mode and
        ((q!='hd' and am=='original') or bool(wr['hd'])) and
        (q!='fast' or bool(wr['fast'])) and
        (not tot or tot==j['seconds'] or bool(wr['append_ok'])))
def need(c,t):
    workers=_live_workers(c,t)
    jobs=list(c.execute("select * from jobs where state in ('queued','running') order by case state when 'running' then 0 else 1 end,created,rowid"))
    tasks={}
    for s in c.execute("""select s.* from shot_tasks s join jobs j on j.id=s.job_id and j.lease=s.epoch where j.state='running'"""):
        tasks.setdefault(s['job_id'],[]).append(s)
    busy={j['wid'] for j in jobs if j['state']=='running'}
    busy.update(s['helper'] for group in tasks.values() for s in group if s['state']=='taken' and s['deadline']>=t)
    idle={w for w in workers if w not in busy}
    missing=[]
    for j in jobs:
        mode=j['precision'] or 'quantized'
        avail=sorted((w for w in idle if _compatible(workers[w],j)),key=lambda w:(not workers[w]['relay'],w))
        if j['state']=='queued':
            owner=avail.pop(0) if avail else None
            if owner:idle.remove(owner)
            else:missing.append(mode)
            saved={r[0] for r in c.execute('SELECT ix FROM shot_checkpoints WHERE job=? AND spec=?',(j['id'],checkpoints.spec(j)))}
            count=min(MAX_CARDS-1,len(set(cuts(json.loads(j['shots']),j['route']))-saved))
        else:
            owner=j['wid']
            group=tasks.get(j['id'],[])
            count=min(sum(s['state']=='open' for s in group),max(0,MAX_CARDS-1-sum(s['state']=='taken' and s['deadline']>=t for s in group)))
        relay=owner is None or (owner in workers and workers[owner]['relay'])
        if owner and (owner not in workers or not workers[owner]['shots']):continue
        for _ in range(count):
            w=next((w for w in avail if workers[w]['shots'] and ((relay and workers[w]['relay']) or (owner and device(w)==device(owner)))),None)
            if w:idle.remove(w);avail.remove(w)
            elif relay:missing.append(mode)
    return missing

def queue_position(c,job,t=None):
    if not job or job['state']!='queued':return None
    mode=job['precision'] or 'quantized'
    ids=[r[0] for r in c.execute("SELECT id FROM jobs WHERE state='queued' AND COALESCE(precision,'quantized')=? ORDER BY created,id",(mode,))]
    return (ids.index(job['id'])+1) if job['id'] in ids else 1

def cluster_summary(c,t):
    workers=_live_workers(c,t)
    running=c.execute("SELECT COUNT(*) FROM jobs WHERE state='running'").fetchone()[0]
    queued=c.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('planning','queued')").fetchone()[0]
    return {'online_count':len(workers),'running_jobs':running,'queued_jobs':queued}

def wait_message(c,mode,t,job=None):
    labels={r[0] for r in c.execute('select stage from pool_signal where mode=? and until>?',(mode,t))}
    workers=_live_workers(c,t)
    pos=queue_position(c,job,t) if job is not None else None
    prefix=f'队列第 {pos} 位 · ' if pos else ''
    compat=[w for w in workers.values() if (_compatible(w,job) if job is not None else w['mode']==mode)]
    if compat:
        busy_ids={r[0] for r in c.execute("SELECT wid FROM jobs WHERE state='running' AND wid IS NOT NULL")}
        busy_ids.update(r[0] for r in c.execute("SELECT s.helper FROM shot_tasks s JOIN jobs j ON j.id=s.job_id AND j.lease=s.epoch WHERE j.state='running' AND s.state='taken' AND s.deadline>=?",(t,)))
        busy_n=sum(1 for w in compat if w['id'] in busy_ids)
        if busy_n:
            if 'loading' in labels:return f'{prefix}已有 {len(compat)} 张 GPU 在线渲染，新卡也在加载模型，空闲后自动接单'
            return f'{prefix}已有 {len(compat)} 张 GPU 正在渲染前序任务，空闲后自动接单'
        return f'{prefix}已有空闲 GPU 在线，即将接单'
    if workers:
        if 'loading' in labels:return f'{prefix}当前在线显卡规格不匹配，Colab 已分配新 GPU 正在准备模型'
        if 'denied' in labels:return f'{prefix}当前在线显卡规格或版本不匹配，且 Colab 暂无额外 GPU，后台会继续重试'
        if 'request' in labels:return f'{prefix}当前在线显卡规格不匹配，正在向 Colab 申请匹配的 GPU'
        return f'{prefix}当前在线显卡规格或版本不匹配，等待匹配的 GPU 接单'
    if 'loading' in labels:return prefix+'Colab 已分配 GPU，正在准备模型，完成后自动接单'
    if 'request' in labels:return prefix+'正在向 Colab 申请 GPU，已有空闲卡可直接接单'
    if 'denied' in labels:return prefix+'Colab 暂无可用 GPU，任务已保留，后台会重试'
    return (f'队列第 {pos} 位，等待 GPU 接单' if pos and pos>1 else None)
