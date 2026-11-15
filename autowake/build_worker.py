"""Adapt a deployed H3 worker for a disposable, noninteractive Colab VM."""
from pathlib import Path
import sys
def build(s):
    import json
    cache=json.loads(Path(__file__).resolve().parents[1].joinpath('model-cache-manifest.json').read_text())
    helper=Path(__file__).with_name('cache_client.py').read_text()
    a=s.index('from google.colab import drive');b=s.index('PRECISION=globals()',a) if 'PRECISION=globals()' in s[a:] else s.index('from huggingface_hub import hf_hub_download',a)
    s=s[:a]+'''import fcntl
_managed_lock=open('/content/h3-managed.lock','a')
fcntl.flock(_managed_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
def exchange(name):
    return next((p/name for p in (Path('/content'),Path('/'),Path.cwd()) if (p/name).is_file()),None)
config=json.loads(exchange('h3managedidentity.json').read_text())
token=config['worker_key'];did=config['worker_id'];BASE='https://withkeya.com/h3-studio'
gpu_present=subprocess.check_output(['nvidia-smi','--query-gpu=name','--format=csv,noheader'],text=True).strip()
if not re.search(r'\\b(?:[AH]100|G4|RTX\\s+PRO\\s+6000\\s+Blackwell)\\b',gpu_present): raise RuntimeError('unsupported GPU')
ROOT=root=Path('/content/h3-managed-data');root.mkdir(exist_ok=True)
models, videos=root/'models', root/'videos'
mounted=Path('/content/drive/MyDrive/MiniMax-H3/models')
if mounted.is_dir():models=mounted
for d in (models,videos):d.mkdir(parents=True,exist_ok=True)
'''+s[b:]
    a=s.index('def drive_result(p):');b=s.index('\ndef ',a+1)+1
    s=s[:a]+'''def drive_result(p,ident):
    def link(fid):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{10,160}',fid):raise ValueError('invalid file id')
        return 'https://drive.google.com/file/d/'+fid+'/view'
    try:return link(os.getxattr(p,'user.drive.id').decode())
    except OSError:pass
    for _ in range(100):
        signal=exchange('h3upload-'+ident+'.json')
        if signal:break
        time.sleep(3)
    else:raise RuntimeError('云盘上传暂不可用')
    info=json.loads(signal.read_text());url=info['url'];size=p.stat().st_size
    if not url.startswith('https://www.googleapis.com/upload/') and not url.startswith('https://content.googleapis.com/upload/'):raise ValueError('invalid upload target')
    with p.open('rb') as f:
        r=requests.put(url,data=f,headers={'Content-Length':str(size),'Content-Type':'video/mp4','Content-Range':f'bytes 0-{size-1}/{size}'},timeout=(10,300),allow_redirects=False)
    r.raise_for_status();fid=r.json()['id'];out=link(fid);os.setxattr(p,'user.drive.id',fid.encode());return out
'''+s[b:]
    assert s.count('drive_result(final)')==2
    s=s.replace('drive_result(final)','drive_result(final,ident)')
    s=s.replace("if PRECISION not in ('quantized','bf16'):","_preparation=Preparation(BASE,config,PRECISION,gpu_present)\nif PRECISION not in ('quantized','bf16'):")
    s=s.replace('bf16_runtime=h3_bf16.prepare()','bf16_runtime=h3_bf16.prepare(_preparation)')

    download="        hf_hub_download(repo_id='Comfy-Org/MiniMax-H3'"
    # shortcut: only pinned quantized video weights are cached; add a BF16 manifest before caching BF16.
    assert s.count(download)==1
    s=s.replace(download,"        try:\n    "+download)
    size_check='        assert path.stat().st_size == size, name'
    assert s.count(size_check)==1
    s=s.replace(size_check,"            assert path.stat().st_size == size, name\n        except Exception:\n            if not restore_cached_model(path, name, BASE, token, did, gpu_present):\n                raise")
    assert s.count('    for name, size in files.items():')==1
    s=s.replace('    for name, size in files.items():','    _preparation.weights(models,files)\n    def prepare_model(pair):\n        name, size = pair')
    s=s.replace("            print('复用：', name)\n            continue","            print('复用：', name)\n            return")
    s=s.replace("    print('四个文件已就绪')","    from concurrent.futures import ThreadPoolExecutor\n    with ThreadPoolExecutor(2) as downloads:\n        list(downloads.map(prepare_model, files.items()))\n    print('四个文件已就绪')\n    _preparation.stage('environment')")
    s=s.replace("    print('准备完成')","    print('准备完成')\n    _preparation.stage('ready')")
    s=s.replace('    g=copy.deepcopy(graph);',"    _preparation.stage('loading')\n    g=copy.deepcopy(graph);")
    s=s.replace("send('/api/worker/sampler',v);last=v;","send('/api/worker/sampler',v);_preparation.stage('ready');last=v;")
    s=s.replace("temp);temp.replace(piece)","temp);temp.replace(piece);_preparation.stage('ready')")
    return 'from h3_preparation import Preparation\nMODEL_CACHE_MANIFEST='+repr(cache)+'\n'+helper+'\n'+s+'\n_h3_worker.join()\n'
if __name__=='__main__':
    import ast
    source=Path(sys.argv[1]).read_text();out=build(source)
    defs=lambda src:{n.name for n in ast.parse(src).body if isinstance(n,ast.FunctionDef)}
    assert (defs(source)-defs(out)) <= {'check_gpu'}, 'rendering functions lost'
    if len(sys.argv)>3:
        backend=Path(sys.argv[3]).read_text()
        out=("import os,sys\nfrom pathlib import Path\n"
             "os.environ['H3_BF16_MODEL_DIR']=str((Path('/mnt/local-scratch') if Path('/mnt/local-scratch').is_mount() else Path('/content'))/'h3-managed-bf16-models')\n"
             "Path('/content/h3_bf16.py').write_text("+repr(backend)+",encoding='utf-8')\n"
             "sys.path.insert(0,'/content')\nH3_STUDIO_MODEL='bf16'\n"+out)
    Path(sys.argv[2]).write_text(out)
