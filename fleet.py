"""Compute the shortage, not just the number of idle GPUs. Shared by API and provisioner."""
import json
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
def need(c,t):
    workers={r['id']:r for r in c.execute("""SELECT w.id,coalesce(m.precision,'quantized') mode,k.shots,k.relay,k.hd,k.fast,k.append_ok
        FROM workers w LEFT JOIN worker_modes m ON m.id=w.id LEFT JOIN worker_caps k ON k.id=w.id
        WHERE w.seen>? AND NOT EXISTS(SELECT 1 FROM worker_drain d WHERE w.id=d.device OR instr(w.id,d.device||'/')=1)""",(t-FRESH,))}
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
        avail=sorted((w for w in idle if workers[w]['mode']==mode and
            (j['quality']!='hd' and (j['audio_mode'] or 'original')=='original' or workers[w]['hd']) and
            (j['quality']!='fast' or workers[w]['fast']) and (not j['total_seconds'] or j['total_seconds']==j['seconds'] or workers[w]['append_ok'])),
            key=lambda w:(not workers[w]['relay'],w))
        if j['state']=='queued':
            owner=avail.pop(0) if avail else None
            if owner:idle.remove(owner)
            else:missing.append(mode)
            count=min(MAX_CARDS-1,len(cuts(json.loads(j['shots']),j['route'])))
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

def wait_message(c,mode,t):
    labels={r[0] for r in c.execute('select stage from pool_signal where mode=? and until>?',(mode,t))}
    if 'loading' in labels:return 'Colab 已分配 GPU，正在准备模型，完成后自动接单'
    if 'request' in labels:return '正在向 Colab 申请 GPU，已有空闲卡可直接接单'
    if 'denied' in labels:return 'Colab 暂无可用 GPU，任务已保留，后台会重试'
    return None
