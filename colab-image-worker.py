"""Persistent original BF16 H3-Image consumer; no quantization or GPU fallback."""
import hashlib,json,os,re,secrets,shutil,subprocess,sys,threading,time,uuid
from pathlib import Path
try:
    from h3_preparation import Preparation,close_all
except ModuleNotFoundError:
    Preparation=None  # Standalone, manually connected workers can run without managed telemetry.
PREPARATION=None

BASE='https://withkeya.com/h3-studio'
REVISION='5f90473023975e6271b0ed1dd84c4e88e4bbc6d3'
DIFFUSERS='43e30028af5120d8b0868cb6975b9fad39cc4fab'
CACHE=(Path('/mnt/local-scratch') if Path('/mnt/local-scratch').is_dir() else Path('/content'))/'h3-image-bf16'/REVISION
ENV=Path('/content/h3-image-bf16-env');PYTHON=str(ENV/'bin/python')

def exchange(name):
    return next((p/name for p in (Path('/content'),Path('/'),Path.cwd()) if (p/name).is_file()),None)

def hardware():
    raw=subprocess.check_output(['nvidia-smi','--query-gpu=name,memory.total,memory.free','--format=csv,noheader,nounits'],text=True)
    rows=[row.rsplit(',',2) for row in raw.strip().splitlines()]
    if len(rows)!=1 or not re.search(r'\b[AH]100\b',rows[0][0]) or int(rows[0][1])<78000 or int(rows[0][2])<73500:
        raise RuntimeError('需要空闲的 A100 80GB 或 H100 80GB，不使用其他显卡或量化模型。')
    import psutil
    if psutil.virtual_memory().available<130*2**30:raise RuntimeError('需要至少 130 GiB 可用主机内存。')
    return rows[0][0].strip(),int(rows[0][1])

def bootstrap():
    hardware()
    if not Path(PYTHON).is_file():subprocess.run([sys.executable,'-m','venv','--without-pip','--system-site-packages',str(ENV)],check=True)
    stamp=ENV/'h3-image-worker-version'
    version=DIFFUSERS+'-torchvision-0.29.1'
    if not stamp.is_file() or stamp.read_text()!=version:
        subprocess.run([sys.executable,'-m','pip','--python',PYTHON,'install','--quiet','diffusers @ git+https://github.com/huggingface/diffusers.git@'+DIFFUSERS,'transformers==5.23.0','accelerate==1.15.0','huggingface_hub==2.4.0','safetensors','sentencepiece','requests','psutil'],check=True)
        subprocess.run([sys.executable,'-m','pip','--python',PYTHON,'install','--quiet','--no-deps','torchvision==0.29.1','--index-url','https://download.pytorch.org/whl/cu130'],check=True)
        stamp.write_text(version)
    subprocess.run([PYTHON,'-u',str(Path(__file__).resolve()),'--run'],check=True)

def prepare():
    from huggingface_hub import HfApi,snapshot_download
    CACHE.mkdir(parents=True,exist_ok=True)
    parts={'text_encoder','tokenizer','processor','transformer_diffusers','vae_diffusers','scheduler'}
    info=HfApi().model_info('MiniMaxAI/MiniMax-H3-Image',revision=REVISION,files_metadata=True)
    manifest={f.rfilename:f.size for f in info.siblings if f.rfilename in {'model_index.json','modular_model_index.json','LICENSE'} or f.rfilename.partition('/')[0] in parts}
    assert info.sha==REVISION and all(any(n.startswith(p+'/') for n in manifest) for p in parts)
    missing=[n for n,size in manifest.items() if not (CACHE/n).is_file() or ((CACHE/n).stat().st_size!=size and n!='modular_model_index.json')]
    if PREPARATION:PREPARATION.weights(CACHE,manifest)
    if shutil.disk_usage(CACHE).free<sum(manifest[n] for n in missing)+8_000_000_000:raise RuntimeError('生图权重缓存磁盘空间不足。')
    if missing:
        print('准备原版权重 GB',round(sum(manifest[n] for n in missing)/1e9,2),flush=True)
        snapshot_download('MiniMaxAI/MiniMax-H3-Image',revision=REVISION,local_dir=str(CACHE),allow_patterns=missing,max_workers=2)
    assert all((CACHE/n).is_file() and (n=='modular_model_index.json' or (CACHE/n).stat().st_size==size) for n,size in manifest.items())
    index=json.loads((CACHE/'modular_model_index.json').read_text())
    for v in index.values():
        if isinstance(v,list) and len(v)==3 and isinstance(v[2],dict):v[2].update(pretrained_model_name_or_path=str(CACHE),revision=None)
    (CACHE/'modular_model_index.json').write_text(json.dumps(index))
    if PREPARATION:PREPARATION.stage('loading')
    import torch
    from diffusers import ComponentsManager,ModularPipeline
    manager=ComponentsManager();manager.enable_auto_cpu_offload(device='cuda',memory_reserve_margin='8GB')
    pipe=ModularPipeline.from_pretrained(str(CACHE),components_manager=manager,local_files_only=True)
    pipe.load_components(dtype=torch.bfloat16)
    assert pipe.processor is not None
    for name in ('transformer','text_encoder','vae'):
        module=getattr(pipe,name)
        assert not getattr(module,'is_quantized',False) and all(p.dtype in (torch.bfloat16,torch.float32) for p in module.parameters())
    return pipe

def run():
    global PREPARATION
    import fcntl,requests,torch
    from PIL import ImageStat
    lock=open('/content/h3-image-worker.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    gpu,memory=hardware()
    identity=exchange('h3managedidentity.json');managed=identity is not None
    if not managed:identity=Path('/content/drive/MyDrive/MiniMax-H3/studio-device.json')
    if not identity.is_file():raise RuntimeError('缺少既有工作端配对信息，请由工作台调度器启动，或先挂载已配对的 Drive。')
    cfg=json.loads(identity.read_text());headers={'Authorization':'Bearer '+cfg['worker_key']}
    if managed and Preparation:PREPARATION=Preparation(BASE,cfg,'image-bf16',gpu,memory)
    worker={'worker_id':cfg['worker_id'],'runtime_id':secrets.token_hex(12),'gpu':gpu,'memory_mib':memory,'precision':'image-bf16','image_43':True}
    out=Path('/content/h3-image-results') if managed else Path('/content/drive/MyDrive/MiniMax-H3-Image-BF16/outputs/studio');out.mkdir(parents=True,exist_ok=True)
    def call(path,body):
        r=requests.post(BASE+'/api/worker/'+path,json={**worker,**body},headers=headers,timeout=(10,60));r.raise_for_status();return r.json()
    def sync_preview(ident,final,fid):
        with final.open('rb') as f:
            r=requests.put(BASE+'/api/worker/media/'+ident+'?drive_id='+fid,data=f,headers={**headers,'Content-Type':'image/png','X-H3-Worker-ID':worker['worker_id'],'X-H3-GPU':gpu,'X-Content-SHA256':hashlib.sha256(final.read_bytes()).hexdigest()},timeout=(10,120))
        r.raise_for_status();(out/(ident+'.synced')).touch()
    # Do not advertise availability until weights and the original pipeline are ready.
    pipe=prepare()
    if PREPARATION:PREPARATION.stage('ready')
    call('heartbeat',{});print('H3-Image 工作端已连接；模型与缓存持续复用。',flush=True)
    active={};guard=threading.Lock();stop=threading.Event()
    def heartbeat():
        while not stop.wait(30):
            with guard:body=dict(active)
            try:call('update',body) if body else call('heartbeat',{})
            except requests.RequestException:pass
    threading.Thread(target=heartbeat,daemon=True).start()
    while True:
        # Recover PNG delivery if the process stopped after committing the Drive result.
        for saved in out.glob('*.drive-id'):
            ident=saved.stem;final=out/(ident+'.png')
            if not re.fullmatch('[a-f0-9]{32}',ident) or not final.is_file() or (out/(ident+'.synced')).exists():continue
            try:sync_preview(ident,final,saved.read_text())
            except requests.RequestException:pass
        try:job=call('claim',{})['job']
        except requests.RequestException:time.sleep(10);continue
        if not job:time.sleep(5);continue
        ident=job['id'];lease={'id':ident,'lease_token':job['lease_token']}
        with guard:active.update(**lease,state='running',progress=5,message='正在生成图片')
        try:
            shot=job['shots'][0];width,height=shot['width'],shot['height']
            if (width,height) not in ((2048,2048),(2048,1536),(1536,2048),(2560,1536),(1536,2560)):raise ValueError('invalid canvas')
            final=out/(ident+'.png');metadata=out/(ident+'.json')
            if not final.is_file():
                started=time.monotonic()
                with torch.inference_mode():image=pipe(prompt=shot['h3_prompt'],height=height,width=width,generator=torch.Generator().manual_seed(int(ident[:8],16)),output='images')[0]
                assert image.size==(width,height) and max(ImageStat.Stat(image.convert('RGB')).stddev)>2
                temp=out/(ident+'.partial.png');image.save(temp);os.replace(temp,final)
                metadata.write_text(json.dumps({'prompt':shot['h3_prompt'],'width':width,'height':height,'seconds':round(time.monotonic()-started,3),'revision':REVISION},ensure_ascii=False))
            with guard:active.update(progress=92,message='图片已生成，正在保存')
            # Delivery retries keep the same PNG and lease; transient network errors never resample.
            while True:
                try:
                    fid_path=out/(ident+'.drive-id')
                    if fid_path.is_file():fid=fid_path.read_text()
                    elif not managed:
                        try:fid=os.getxattr(final,'user.drive.id').decode()
                        except OSError:time.sleep(5);continue
                        if not re.fullmatch(r'[\w-]{10,160}',fid):time.sleep(5);continue
                        fid_path.write_text(fid)
                    else:
                        signal=exchange('h3upload-'+ident+'.json')
                        if signal is None:time.sleep(5);continue
                        url=json.loads(signal.read_text())['url']
                        if not url.startswith(('https://www.googleapis.com/upload/','https://content.googleapis.com/upload/')):raise ValueError('invalid upload target')
                        size=final.stat().st_size
                        with final.open('rb') as f:
                            r=requests.put(url,data=f,headers={'Content-Length':str(size),'Content-Type':'image/png','Content-Range':f'bytes 0-{size-1}/{size}'},timeout=(10,300),allow_redirects=False)
                        r.raise_for_status();fid=r.json()['id']
                        if not re.fullmatch(r'[\w-]{10,160}',fid):raise ValueError('invalid Drive id')
                        fid_path.write_text(fid)
                    call('update',{**lease,'state':'done','progress':100,'message':'图片已保存','result_url':'https://drive.google.com/file/d/'+fid+'/view'})
                    with guard:active.clear()
                    sync_preview(ident,final,fid);print('IMAGE_DONE',ident,flush=True);break
                except requests.RequestException as e:
                    if e.response is not None and e.response.status_code in (403,409):raise
                    time.sleep(10)
        except Exception as e:
            print('IMAGE_ERROR',ident,type(e).__name__,flush=True)
            try:call('update',{**lease,'state':'failed','message':'图片生成或保存失败，请重试；已生成的文件保留在执行端。'})
            except requests.RequestException:pass
        finally:
            with guard:active.clear()

if __name__=='__main__':
    try:run() if '--run' in sys.argv else bootstrap()
    except BaseException:
        if Preparation:close_all(failed=True)
        raise
