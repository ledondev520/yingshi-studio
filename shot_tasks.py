"""Parallel cutaways. The parent runtime always owns continuity and final assembly."""
import fleet
import checkpoints
import hmac
import json
import secrets

TABLE = """CREATE TABLE IF NOT EXISTS shot_tasks(
 job_id TEXT, epoch TEXT, ix INTEGER, state TEXT, helper TEXT, ticket TEXT,
 deadline INTEGER, size INTEGER, PRIMARY KEY(job_id, ix))"""
TTL = 100
MAX_HELPERS = fleet.MAX_CARDS-1

def offer(c, job):
    # A new parent lease fences off results of an earlier attempt.
    c.execute('DELETE FROM shot_tasks WHERE job_id=?',(job['id'],))
    shots=json.loads(job['shots'])
    if len(shots)<3 or job['route']=='continuous': return
    for ix, shot in enumerate(shots):
        if c.execute('SELECT 1 FROM shot_checkpoints WHERE job=? AND ix=? AND spec=?',(job['id'],ix,checkpoints.spec(job))).fetchone():continue
        if ix and shot.get('cut') is True:
            c.execute("INSERT INTO shot_tasks(job_id,epoch,ix,state) VALUES(?,?,?,'open')",
                      (job['id'],job['lease'],ix))

def busy(c, wid, t):
    return c.execute("""SELECT 1 FROM shot_tasks s JOIN jobs j ON j.id=s.job_id
        WHERE s.helper=? AND s.state='taken' AND s.deadline>=? AND s.epoch=j.lease
        AND j.state='running'""",(wid,t)).fetchone()

def claim(c,wid,d,t,jobrow):
    # A paired device identifies the Drive holding its studio-device.json. Different
    # runtimes of that same device can exchange pieces there without public URLs.
    if '/' not in wid or d.get('shot_v1') is not True: return
    row=c.execute("""SELECT s.job_id,s.ix FROM shot_tasks s JOIN jobs j ON j.id=s.job_id
        WHERE j.state='running' AND j.lease=s.epoch AND j.wid!=?
        AND COALESCE(j.precision,'quantized')=? AND (substr(j.wid,1,instr(j.wid,'/'))=? OR (? AND EXISTS(SELECT 1 FROM worker_caps k WHERE k.id=j.wid AND k.relay=1))) AND s.state='open'
        AND (coalesce(j.quality,'standard')='standard' OR (j.quality='fast' AND ?) OR (j.quality='hd' AND ?))
        AND (SELECT count(*) FROM shot_tasks h WHERE h.job_id=s.job_id AND h.epoch=s.epoch
             AND h.state='taken' AND h.deadline>=?) < ?
        ORDER BY j.created, s.ix LIMIT 1""",
        (wid,d.get('precision','quantized'),wid.split('/')[0]+'/',int(d.get('relay_v1') is True),int(d.get('turbo8') is True),int(d.get('finish_v2') is True),t,MAX_HELPERS)).fetchone()
    if not row:return
    jid,ix=row
    ticket=secrets.token_hex(16)
    c.execute("UPDATE shot_tasks SET state='taken',ticket=?,helper=?,deadline=? WHERE job_id=? AND ix=?",
              (ticket,wid,t+TTL,jid,ix))
    job=jobrow(c.execute("SELECT * FROM jobs WHERE id=?",(jid,)).fetchone(),True)
    del job['lease_token']
    relay=bool(d.get('relay_v1') and c.execute('select 1 from worker_caps k join jobs j on j.wid=k.id where j.id=? and k.relay=1',(jid,)).fetchone())
    if relay:c.execute('insert into shot_relay(ticket,created) values(?,?)',(ticket,t))
    return {'job':job,'ix':ix,'ticket':ticket,'relay':relay}

def handle(c,op,wid,d,t,fail):
    ix=d.get('ix');jid=d.get('id')
    if type(ix) is not int or not isinstance(jid,str):fail(400,'无效镜号')
    r=c.execute("""SELECT s.*,j.wid AS owner,j.lease AS current FROM shot_tasks s
        JOIN jobs j ON j.id=s.job_id WHERE s.job_id=? AND s.ix=? AND j.state='running'
        AND j.lease=s.epoch""",(jid,ix)).fetchone()
    if not r:fail(409,'分镜任务已过期')
    if op=='status':
        if wid!=r['owner'] or not hmac.compare_digest(r['current'],str(d.get('lease_token',''))):fail(409,'主卡租约失效')
        if r['state']=='ready':
            blob=c.execute('select sha from shot_relay where ticket=?',(r['ticket'],)).fetchone()
            return {'state':'ready','ticket':r['ticket'],'size':r['size'],**({'sha':blob[0]} if blob else {})}
        if r['state']=='taken' and r['deadline']>=t:return {'state':'waiting'}
        c.execute("UPDATE shot_tasks SET state='local' WHERE job_id=? AND ix=?",(jid,ix))
        return {'state':'local'}
    if op not in ('pulse','ready','release'):fail(404,'未知分镜操作')
    if wid!=r['helper'] or r['state']!='taken' or r['deadline']<t or not hmac.compare_digest(r['ticket'],str(d.get('ticket',''))):
        fail(409,'副卡租约失效')
    state={'ready':'ready','pulse':'taken','release':'local'}[op]
    size=d.get('size') if op=='ready' else None
    if op=='ready' and (type(size) is not int or size<=0):fail(400,'片段为空')
    blob=c.execute('select size,sha from shot_relay where ticket=?',(r['ticket'],)).fetchone()
    if op=='ready' and blob and (not blob['sha'] or blob['size']!=size):fail(409,'片段尚未完整上传')
    c.execute("UPDATE shot_tasks SET state=?,deadline=?,size=? WHERE job_id=? AND ix=?",(state,t+TTL,size,jid,ix))
    return {'ok':True}
