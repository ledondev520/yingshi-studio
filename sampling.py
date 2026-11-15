"""Per-shot measured progress. This is telemetry, never a job heartbeat or lease renewal."""
import hashlib,re
TABLE="""CREATE TABLE IF NOT EXISTS sampler_ticks(job TEXT,epoch TEXT,ix INTEGER,wid TEXT,prompt TEXT,done INTEGER,total INTEGER,rate REAL,seen INTEGER,PRIMARY KEY(job,ix))"""
def accept(c,wid,d,t,fail):
    part=d.get('part',''); tag=d.get('tag','');cid=d.get('cid','')
    k,n,rate=(d.get(f) for f in ('done','total','rate'))
    if not all(isinstance(v,str) for v in (part,tag,cid)) or not re.fullmatch(r'[0-9a-f]{24}',tag) or not re.fullmatch(r'[a-fA-F0-9-]{16,64}',cid) or type(k)!=int or type(n)!=int or not 0<=k<=n<=250 or n<1 or type(rate) not in (float,int) or not 0<=rate<=86400:fail(400,'采样记录无效')
    if re.fullmatch(r'\d{2}',part):
        ix=int(part)
        r=c.execute("select id,lease from jobs where wid=? and state='running' and deadline>=? and message like ?",(wid,t,f'生成第 {ix+1}/% 镜')).fetchone()
    elif re.fullmatch(r'helper-[a-f0-9]{32}',part):
        r=c.execute("select j.id,j.lease,s.ix from shot_tasks s join jobs j on j.id=s.job_id and j.lease=s.epoch where j.state='running' and s.helper=? and s.state='taken' and s.ticket=? and s.deadline>=?",(wid,part[7:],t)).fetchone()
        ix=r['ix'] if r else None
    else:fail(400,'分镜标识无效')
    if not r or hashlib.sha256(r['id'].encode()).hexdigest()[:24]!=tag:fail(409,'分镜已切换')
    c.execute("""insert into sampler_ticks values(?,?,?,?,?,?,?,?,?) on conflict(job,ix) do update set
        epoch=excluded.epoch,wid=excluded.wid,prompt=excluded.prompt,done=excluded.done,total=excluded.total,rate=excluded.rate,seen=excluded.seen
        where epoch!=excluded.epoch or prompt!=excluded.prompt or excluded.done>=done""",(r['id'],r['lease'],ix,wid,cid,k,n,rate,t))
def visible(c,r,t):
    if r['state']!='running':return []
    m=re.fullmatch(r'生成第 (\d+)/(\d+) 镜',r['message'] or '')
    out=[]
    for v in c.execute("""select p.*,exists(select 1 from shot_tasks s where s.job_id=p.job and s.epoch=p.epoch and s.ix=p.ix and s.helper=p.wid and s.state='taken' and s.deadline>=?) as assisting
        from sampler_ticks p where p.job=? and p.epoch=? order by p.ix""",(t,r['id'],r['lease'])):
        if not ((v['wid']==r['wid'] and m and int(m[1])==v['ix']+1) or v['assisting']):continue
        out.append(dict(shot=v['ix']+1,done=v['done'],total=v['total'],sec_per_step=v['rate'],seen=v['seen']))
    return out
