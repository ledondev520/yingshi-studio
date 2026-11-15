"""Image routing, Carbon validation and private PNG delivery without paid calls."""
import io, json, tempfile, threading, urllib.request, urllib.error, uuid, zlib, struct
from pathlib import Path
from http.server import ThreadingHTTPServer
import server

def run():
 with tempfile.TemporaryDirectory() as tmp:
  server.DATA=server.KEYS=Path(tmp);server.setup()
  (server.KEYS/'worker-key').write_text('image-test-only')
  http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
  server.ORIGIN=f'http://127.0.0.1:{http.server_port}'
  threading.Thread(target=http.serve_forever,daemon=True).start()
  def req(p,d=None,cookie='',status=200,method=None,headers=None):
   hdr={'Origin':server.ORIGIN,'Content-Type':'application/json','Cookie':cookie,**(headers or {})}
   data=d if isinstance(d,bytes) else json.dumps(d).encode() if d is not None else None
   try:r=urllib.request.urlopen(urllib.request.Request(server.ORIGIN+'/api/'+p,data=data,headers=hdr,method=method),timeout=5)
   except urllib.error.HTTPError as e:r=e
   raw=r.read();assert r.code==status,(p,r.code,raw)
   return (json.loads(raw) if 'json' in r.headers.get('Content-Type','') else raw),r.headers
  try:
   cookie=req('state')[1]['Set-Cookie'].split(';')[0];other=req('state')[1]['Set-Cookie'].split(';')[0]
   data={'kind':'image','aspect':'square','prompt':'清晨山间的茶馆，招牌写“山间茶事”','request_id':uuid.uuid4().hex}
   j=req('create',data,cookie)[0]['job'];assert j['kind']=='image' and j['seconds']==0
   assert req('create',data,cookie)[0]['job']['id']==j['id']
   req('create',{**data,'aspect':'portrait'},cookie,status=409)
   req('create',{**data,'aspect':'bad'},cookie,status=400)
   req('creation/'+j['id'],cookie=other,status=404)
   with server.db() as c:
    c.execute("UPDATE jobs SET state='queued',shots=?,quality='standard',audio_mode='original' WHERE id=?",(json.dumps([{'prompt':'茶馆','h3_prompt':'A quiet teahouse','width':2048,'height':2048,'seconds':0}]),j['id']))
    assert server.fleet.need(c,server.now())==['image-bf16']
   auth={'Authorization':'Bearer image-test-only'}
   worker={'worker_id':'image-test','runtime_id':'1'*24,'gpu':'NVIDIA A100-SXM4-80GB','precision':'image-bf16','memory_mib':81920}
   assert not req('worker/claim',{**worker,'precision':'bf16'},headers=auth)[0]['job']
   req('worker/claim',{**worker,'gpu':'NVIDIA A100-SXM4-40GB','memory_mib':40960},headers=auth,status=422)
   req('worker/claim',{**worker,'gpu':'NVIDIA RTX PRO 6000 Blackwell'},headers=auth,status=422)
   job=req('worker/claim',worker,headers=auth)[0]['job'];assert job['id']==j['id']
   link='https://drive.google.com/file/d/image_test_12345/view'
   req('worker/update',{**worker,'id':job['id'],'lease_token':job['lease_token'],'state':'done','result_url':link},headers=auth)
   def chunk(k,b):return struct.pack('>I',len(b))+k+b+struct.pack('>I',zlib.crc32(k+b))
   png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\0\xff\0\0'))+chunk(b'IEND',b'')
   upload={**auth,'Content-Type':'image/png','X-H3-Worker-ID':worker['worker_id'],'X-H3-GPU':worker['gpu']}
   path='worker/media/'+job['id']+'?drive_id=image_test_12345'
   req(path,b'invalid',method='PUT',headers=upload,status=422)
   req(path,png,method='PUT',headers=upload)
   req('media/'+job['id'],cookie=other,status=404)
   saved,h=req('media/'+job['id'],cookie=cookie);assert saved==png and h['Content-Type']=='image/png'
   assert req('media/'+job['id']+'?token='+j['watch_token'])[0]==png
   assert req('creation/'+j['id'],cookie=cookie)[0]['job']['media_ready']
   from image_director import validate
   assert validate({'description':'茶馆','prompt':'A detailed visual scene. '*15},'square')['shots'][0]['width']==2048
   for bad in ({},{'description':'茶馆','prompt':'short'},{'description':'茶馆','prompt':42}):
    try:validate(bad,'square')
    except ValueError:pass
    else:raise AssertionError('Invalid Carbon output accepted')
   print('PASS image create/idempotency, GPU/routing, PNG validation, visitor isolation, watch access, Carbon schema')
  finally:http.shutdown();http.server_close()

if __name__=='__main__':run()
