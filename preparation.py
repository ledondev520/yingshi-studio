"""Measured preparation status, separate from GPU availability and task leases."""
TABLE='''CREATE TABLE IF NOT EXISTS pool_preparation(
 device TEXT PRIMARY KEY,mode TEXT,stage TEXT,bytes_done INTEGER,bytes_total INTEGER,
 files_done INTEGER,files_total INTEGER,reused_bytes INTEGER,seen INTEGER)'''
STAGES={'environment','download','copying','cache','loading','ready','failed'}

def accept(c,device,mode,data,t,fail):
    stage=data.get('stage')
    fields=('bytes_done','bytes_total','files_done','files_total','reused_bytes')
    values=[data.get(k) for k in fields]
    if stage not in STAGES or any(type(v) is not int or v<0 for v in values):fail(400,'无效的准备进度')
    done,total,count,files,reused=values
    if not reused<=done<=total<=2_000_000_000_000 or not count<=files<=10000:fail(400,'无效的准备进度')
    if stage not in ('download','copying','cache') and any(values):fail(400,'此阶段不能填入下载进度')
    if stage=='ready':c.execute('DELETE FROM pool_preparation WHERE device=?',(device,))
    else:c.execute('INSERT OR REPLACE INTO pool_preparation VALUES(?,?,?,?,?,?,?,?,?)',(device,mode,stage,*values,t))

def visible(c,t,mode=None,device=None):
    rows=c.execute('''SELECT p.* FROM pool_preparation p WHERE (? IS NULL OR p.mode=?)
       AND (? IS NULL OR p.device=?) AND (
         EXISTS(SELECT 1 FROM pool_signal s WHERE s.device=p.device AND s.mode=p.mode AND s.stage='loading' AND s.until>?)
         OR (p.stage='loading' AND EXISTS(SELECT 1 FROM jobs j WHERE j.state='running' AND instr(j.wid,p.device||'/')=1)))
       ORDER BY p.seen DESC,p.device''',(mode,mode,device,device,t))
    return [{k:r[k] for k in ('mode','stage','bytes_done','bytes_total','files_done','files_total','reused_bytes','seen')} |
            {'stale':t-r['seen']>90} for r in rows]
