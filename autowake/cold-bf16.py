import os,sys
from pathlib import Path
os.environ['H3_BF16_MODEL_DIR']=str((Path('/mnt/local-scratch') if Path('/mnt/local-scratch').is_mount() else Path('/content'))/'h3-managed-bf16-models')
Path('/content/h3_bf16.py').write_text('"""Official FL2VA BF16 backend for one supported GPU with at least 80 GB. Invoked only by BF16 workers."""\nimport json\nimport math\nimport os\nimport re\nimport shutil\nimport subprocess\nimport sys\nimport tempfile\nimport time\nfrom concurrent.futures import ThreadPoolExecutor\nfrom pathlib import Path\n\nREV = "42ed227ee7df40d41602854ae760620d6eb651fe"\nDIFFUSERS = "84532a058702c74ae5d1dd6ceb05683fc5d5e768"\nREPO = "MiniMaxAI/MiniMax-H3"\nPARTS = {"transformer", "text_encoder", "vae", "audio_vae", "scheduler", "audio_scheduler", "tokenizer", "processor"}\nFILES = {"LICENSE", "model_index.json", "modular_model_index.json"}\n\n\ndef prepare():\n    import psutil\n    from huggingface_hub import HfApi, snapshot_download\n    info = subprocess.check_output(["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"], text=True)\n    name, total, free = info.splitlines()[0].rsplit(",", 2)\n    if not re.search(r"\\b(?:[AH]100|G4|RTX\\s+PRO\\s+6000\\s+Blackwell)\\b", name) or int(total)<78000 or int(free)<73500:\n        raise RuntimeError("BF16 需要一张空闲的 A100/H100 80 GB 或 G4 96 GB。")\n    ram = psutil.virtual_memory().available\n    for cap,used in (("/sys/fs/cgroup/memory.max","/sys/fs/cgroup/memory.current"),("/sys/fs/cgroup/memory/memory.limit_in_bytes","/sys/fs/cgroup/memory/memory.usage_in_bytes")):\n        if Path(cap).exists() and Path(used).exists() and (v:=Path(cap).read_text().strip()).isdigit():\n            stat=Path(used).with_name("memory.stat")\n            fields=dict(line.split() for line in stat.read_text().splitlines()) if stat.exists() else {}\n            reclaim=int(fields.get("inactive_file",fields.get("total_inactive_file",0)))\n            ram = min(ram, int(v)-int(Path(used).read_text())+reclaim)\n    if ram < 145*2**30: raise RuntimeError("BF16 工作端支持首帧续拍，请启用高 RAM（建议至少 145 GiB 可用）。")\n    data = Path(os.environ.get("H3_BF16_MODEL_DIR","/content/drive/MyDrive/MiniMax-H3-BF16/models")) / REV\n    data.mkdir(parents=True,exist_ok=True)\n    manifest = {x.rfilename:x.size for x in HfApi().model_info(REPO,revision=REV,files_metadata=True).siblings\n                if x.rfilename in FILES or x.rfilename.partition("/")[0] in PARTS}\n    if not all(any(f.startswith(d+"/") for f in manifest) for d in PARTS) or not all(isinstance(v,int) for v in manifest.values()):\n        raise ValueError("权重清单不完整")\n    def pending(root):\n        return [f for f,n in manifest.items() if not (root/f).is_file() or (root/f).stat().st_size!=n]\n    missing = pending(data)\n    if missing:\n        if shutil.disk_usage(data).free < sum(manifest[f] for f in missing)+5_000_000_000: raise RuntimeError("Drive 空间不足")\n        snapshot_download(REPO,revision=REV,allow_patterns=sorted(manifest),local_dir=str(data),max_workers=2)\n    if pending(data): raise RuntimeError("Drive 权重不完整")\n    scratch=Path("/mnt/local-scratch")\n    src=data\n    if scratch.is_mount() and scratch not in data.parents:\n        src=scratch/("h3-bf16-"+REV[:7]);src.mkdir(exist_ok=True)\n        missing=pending(src)\n        if shutil.disk_usage(src).free < sum(manifest[f] for f in missing)+5_000_000_000: raise RuntimeError("本地暂存盘空间不足")\n        def copy(f):\n            dest=src/f;dest.parent.mkdir(parents=True,exist_ok=True)\n            tmp=dest.with_name(dest.name+".part");shutil.copyfile(data/f,tmp)\n            if tmp.stat().st_size!=manifest[f]: raise RuntimeError("暂存分片不完整")\n            tmp.replace(dest)\n        with ThreadPoolExecutor(max_workers=5) as pool:list(pool.map(copy,missing))\n    venv=Path("/content/minimax-h3-bf16-env"); py=venv/"bin/python"\n    if not py.exists():subprocess.run([sys.executable,"-m","venv","--without-pip","--system-site-packages",str(venv)],check=True)\n    marker=venv/("studio-ready-"+DIFFUSERS)\n    if not marker.is_file():\n        subprocess.run([str(py),"-m","pip","install","--quiet","diffusers @ git+https://github.com/huggingface/diffusers.git@"+DIFFUSERS,"transformers==5.22.2","accelerate>=1.6","huggingface_hub","av","safetensors","sentencepiece"],check=True)\n        subprocess.run([str(py),"-c","import av,torch,torchaudio; from diffusers import ComponentsManager,ModularPipeline; from diffusers.utils.export_utils import encode_video; assert torch.cuda.is_bf16_supported()"],check=True)\n        marker.touch()\n    return str(py),str(src)\n\n\ndef frame_plan(seconds):\n    target=round(seconds*24)\n    native=max(124,min(345,5+17*math.ceil((target-5)/17)))\n    return native,target\n\n\ndef generate(cfg):\n    import gc\n    import torch\n    from PIL import Image\n    from diffusers import ComponentsManager,ModularPipeline\n    from diffusers.utils.export_utils import encode_video\n    weights=cfg["weights"]\n    def manager():\n        m=ComponentsManager();m.enable_auto_cpu_offload(device="cuda:0",memory_reserve_margin="12GB");return m\n    def chunk_ff(fn):\n        def forward(x):\n            for span in x.split(1024,dim=-2):span.copy_(fn(span))\n            return x\n        return forward\n    args=dict(width=cfg["width"],height=cfg["height"],num_frames=cfg["frames"],num_inference_steps=50,\n              generator=torch.Generator().manual_seed(cfg["seed"]),output=["videos","audio","sampling_rate"])\n    with torch.inference_mode():\n        if not cfg["first"]:\n            flow=ModularPipeline.from_pretrained(weights).blocks.get_workflow("t2va")\n            em=manager()\n            encoder=flow.sub_blocks.pop("text_encoder").init_pipeline(weights,components_manager=em)\n            encoder.load_components(dtype=torch.bfloat16)\n            args["state"]=encoder(prompt=cfg["prompt"])\n            del encoder,em\n            gc.collect();torch.cuda.empty_cache()\n            m=manager();pipe=flow.init_pipeline(weights,components_manager=m)\n        else:\n            m=manager();pipe=ModularPipeline.from_pretrained(weights,workflow="fl2va",components_manager=m)\n            args.update(prompt=cfg["prompt"],image=Image.open(cfg["first"]).convert("RGB"))\n        pipe.load_components(dtype=torch.bfloat16)\n        for block in pipe.transformer.transformer_blocks:block.ff.forward=chunk_ff(block.ff.forward)\n        pipe.vae.enable_tiling(tile_sample_min_height=512,tile_sample_min_width=512)\n        counter=[0]\n        def step(*unused):\n            counter[0]+=1\n            Path(cfg["step_file"]).write_text(str(counter[0]))\n        pipe.transformer.register_forward_hook(step)\n        result=pipe(**args)\n        encode_video(result["videos"][0],fps=24,output_path=cfg["output"],audio=result["audio"][0],audio_sample_rate=result["sampling_rate"])\n\n\ndef render(runtime,job,ix,piece,previous,update,size,seed):\n    if job.get("precision")!="bf16":raise ValueError("BF16 执行端拒绝其他模型任务")\n    seconds=float(job["shots"][ix]["seconds"])\n    native,n=frame_plan(seconds)\n    with tempfile.TemporaryDirectory(dir="/content") as td:\n        tmp=Path(td);base=tmp/"original.mp4";first=tmp/"first.png";steps=tmp/"step"\n        if previous and Path(previous).suffix==".png":first.write_bytes(Path(previous).read_bytes())\n        elif previous:\n            subprocess.run(["ffmpeg","-loglevel","error","-sseof","-0.18","-i",str(previous),"-frames:v","1",str(first)],check=True)\n        cfg=dict(weights=runtime[1],first=str(first) if previous else None,seed=seed,frames=native,\n                 prompt=job["shots"][ix]["prompt"],width=size[0],height=size[1],step_file=str(steps),output=str(base))\n        log=Path("/content/h3-bf16-worker-last.log")\n        with log.open("w") as out:\n            proc=subprocess.Popen([runtime[0],"-u",str(Path(__file__).resolve())],stdin=subprocess.PIPE,stdout=out,stderr=subprocess.STDOUT,text=True,\n                env={**os.environ,"HF_HUB_OFFLINE":"1","TRANSFORMERS_OFFLINE":"1","PYTORCH_CUDA_ALLOC_CONF":"expandable_segments:True"})\n            try:\n                proc.stdin.write(json.dumps(cfg));proc.stdin.close()\n                while True:\n                    k=steps.read_text().strip() if steps.is_file() else ""\n                    note="去噪 "+k+"/49" if k.isdigit() else "加载模型"\n                    update("running",4,f"BF16 · 第 {ix+1}/{len(job[\'shots\'])} 镜 · {note}")\n                    try:rc=proc.wait(timeout=25);break\n                    except subprocess.TimeoutExpired:pass\n                if rc:raise RuntimeError("BF16 生成失败，详见执行端日志")\n            finally:\n                if proc.poll() is None:\n                    proc.terminate()\n                    try:proc.wait(timeout=10)\n                    except subprocess.TimeoutExpired:proc.kill();proc.wait()\n        # Shots shorter than the model\'s minimum are trimmed; only the small gap at the 15 s limit is retimed.\n        ratio=max(1,n/native); film=tmp/"shot.mp4"\n        # Separate finite streams avoid a scheduler deadlock between tpad, fps and apad.\n        vfile=tmp/"v.mp4";afile=tmp/"a.m4a"\n        def ff(*opts):subprocess.run(["ffmpeg","-nostdin","-loglevel","error","-y",*map(str,opts)],timeout=900,check=True)\n        ff("-i",base,"-an","-vf",f"setpts=PTS*{ratio},fps=24,tpad=stop_mode=clone:stop_duration=0.2","-frames:v",n,"-c:v","libx264","-preset","fast","-crf","17",vfile)\n        ff("-i",base,"-vn","-af",f"atempo={1/ratio},apad=pad_dur=1","-t",seconds,"-c:a","aac","-b:a","192k",afile)\n        ff("-i",vfile,"-i",afile,"-map","0:v:0","-map","1:a:0","-c","copy","-t",seconds,"-movflags","+faststart",film)\n        dest=piece.with_suffix(".part");shutil.copyfile(film,dest);dest.replace(piece)\n\n\nif __name__=="__main__":generate(json.load(sys.stdin))\n',encoding='utf-8')
sys.path.insert(0,'/content')
H3_STUDIO_MODEL='bf16'
MODEL_CACHE_MANIFEST={'diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors': {'size': 20970379616, 'sha256': 'e889202c41dafb67b10d67b97f0d8541508036a6090af23425a5c2615d03c47a'}, 'text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors': {'size': 15687142551, 'sha256': '35a88d51044231fe332301d7a62aa81e3f2cba62febeb446e2c1e3e0ef76f2c6'}, 'vae/minimax_h3_audio_vae_fp32.safetensors': {'size': 605254808, 'sha256': '8e505d95dd1561d47abd43d4238fd40d9bb1ae9e147ed0a4cba778d76ae4db48'}, 'vae/minimax_h3_video_vae_int8_convrot.safetensors': {'size': 2811065184, 'sha256': '52a2c8c73583c86e4f41cdcce3a6ad0ea562987bc0bf3d60a0cef5f5c8e60c0e'}}
"""Restore a pinned model from the private cache; incomplete files never replace models."""
def restore_cached_model(path, name, base, worker_key, worker_id, gpu):
    import hashlib
    import time
    import requests
    meta = MODEL_CACHE_MANIFEST.get(name)
    if not meta:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + '.cache.part')
    for attempt in range(3):
        start = partial.stat().st_size if partial.exists() else 0
        if start > meta['size']:
            partial.unlink(); start = 0
        if start < meta['size']:
            headers = {'Authorization': 'Bearer ' + worker_key, 'X-H3-Worker-ID': worker_id, 'X-H3-GPU': gpu}
            if start:
                headers['Range'] = f'bytes={start}-'
            try:
                with requests.get(base + '/api/worker/model-cache/' + meta['sha256'], headers=headers,
                                  stream=True, timeout=(10, 60), allow_redirects=False) as response:
                    if response.status_code == 404:
                        return False
                    if response.status_code in (429, 503):
                        time.sleep(2 * (attempt + 1)); continue
                    response.raise_for_status()
                    if response.status_code != (206 if start else 200):
                        raise ValueError('Cache range not honored')
                    if (response.headers.get('X-Content-SHA256') != meta['sha256']
                            or int(response.headers.get('Content-Length', -1)) != meta['size'] - start):
                        raise ValueError('Cache metadata mismatch')
                    with partial.open('ab' if start else 'wb') as out:
                        count = start
                        for chunk in response.iter_content(1024 * 1024):
                            count += len(chunk)
                            if count > meta['size']:
                                raise ValueError('Cache response too long')
                            out.write(chunk)
            except requests.RequestException:
                continue
        if partial.exists() and partial.stat().st_size == meta['size']:
            with partial.open('rb') as stream:
                valid = hashlib.file_digest(stream, 'sha256').hexdigest() == meta['sha256']
            if not valid:
                partial.unlink()
                raise ValueError('Model cache checksum mismatch')
            partial.replace(path)
            print('持久缓存已恢复：', name, flush=True)
            return True
    print('持久缓存暂不可用，改用原始模型源：', name, flush=True)
    return False

"""Keep one instrumental bed across independently rendered H3 shots. Requires numpy and ffmpeg."""
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import wave

import numpy as np

SR = 48000
HOP = 480   # 10 ms per analysis frame
NEG = r'(?:不需要|不想要|不要|不能有|不加|不带|不含|不用|不配|没有|无需|不许|无|禁止|去除|去掉|避免|排除|禁用|取消|去掉所有)(?:任何|一切|额外|背景|原创|明显|额外的|任何形式的|任何的|的|\s)*'
NOMUSIC = re.compile(NEG+r'(?:配乐|伴奏|音乐|bgm|乐曲|背景乐)|(?:音乐|配乐|伴奏|bgm)(?:不要|关闭|静音)|(?:只(?:有|要|保留)|仅(?:有|要|保留)?)(?:环境(?:音|声)|现场(?:音|声)|自然(?:音|声)|雨声|脚步声|人声|对白)|静音|无声(?:片|视频)|silent film|\b(?:without|no|mute(?:d)?|remove)\s+(?:any\s+|background\s+)?(?:music|soundtrack|instrumental|bgm|audio)\b|\bambient sound only\b',re.I)
VOICE_CN = r'(?:人声|对话|对白|台词|说话|讲话|演讲|旁白|口播|独白|朗读|朗诵|聊天|交谈|谈话|歌唱|歌声|唱歌|演唱|歌词|吟唱|哼唱|开口唱|唱出|清唱|合唱|齐唱|主唱|说唱|配音|念白|解说|喊叫|报幕|对嘴|对口型|口型|唱跳)'
VOICE_EN = r'(?:vocals?|lyrics?|dialog(?:ue)?|speech|speaking|spoken|narrat(?:e|ed|ion|or)|voice(?:over)?|sing(?:ing|s|er)?|choir|rapp?(?:er|ing)?|lip[- ]?sync(?:ing)?|conversation|talk(?:ing|s)?|chant(?:ing)?|shout(?:ing)?)'
ORIGINAL = re.compile(r'(?:保留|沿用|保持)(?:全部|每一?[段个镜头]*|各[段镜头]*|所有|各自|每镜|现场|整套|分镜|的|\s)*(?:原声|原音|原配乐|原音轨)|(?:每一?[段镜]|各镜头).{0,5}(?:不同音乐|换曲|换歌|切歌)|不(?:需要|要|用)?(?:统一|连续|循环|更换|替换|修改)(?:配乐|背景音乐|BGM|原声|音乐)|(?:纯|仅有)(?:环境音|环境声|自然声)|\b(?:keep|preserve)\s+(?:the\s+)?original\s+(?:sound|audio|music)\b',re.I)
VOICE_NEGATED = re.compile(r'(?:不(?:许|要|用|需要)?|没有|无需|禁止|无|避免|全程不)(?:(?:任何|一切|任何形式的|明显的|\s)*)'+VOICE_CN+r'(?:(?:[、/]|和|或|以及|及)'+VOICE_CN+r')*|'+NEG+VOICE_CN+r'|\b(?:without|no|non)(?:\s+(?:any|sung|spoken|human))*[\s-]+'+VOICE_EN+r'\b',re.I)
VOICE = re.compile(VOICE_CN+r'|[“「『《].{1,90}?[”」』》]|(?:他说|她说|说：|说:|问道|答道|台上唱|唱着|唱一|唱首|唱这|唱那|唱首歌|唱起|唱的歌|唱段)|\b'+VOICE_EN+r'\b',re.I)
MUSICAL = re.compile(r'舞蹈|跳舞|热舞|街舞|编舞|群舞|双人舞|独舞|芭蕾|舞曲|跳.{0,6}舞|(?:无词|纯)(?:音乐|器乐)|器乐(?:演奏|合奏|独奏)|纯伴奏|\b(?:dance|dancing|choreograph(?:y|ed)|ballet|instrumental)\b',re.I)

def h3_music_policy(prompt, mode='auto', shot_prompts=()):
    """Return (replace_with_first_instrumental_bed: bool, reason: str). Protected content wins."""
    if mode in ('original','preserve','native','keep'):
        return False, '已选保留每镜原声'
    if mode not in ('auto','continuous'):
        raise ValueError('音乐模式应为 auto、continuous 或 original')
    if isinstance(shot_prompts,str): shot_prompts=[shot_prompts]
    raw='。'.join([str(prompt)]+[str(x.get('prompt','') if isinstance(x,dict) else x) for x in (shot_prompts or [])])
    if ORIGINAL.search(raw):return False,'提示词要求保留原声音轨或不统一配乐'
    if NOMUSIC.search(raw):return False,'有无配乐、静音或只留环境声的要求'
    # “无对白/人声” is an absence, not an instruction to preserve speech.
    # The workbench's generic story template permits an action *or* dialogue. It does not
    # request a line. Other references to dialogue must still prevent audio replacement.
    generic='交代场景和主要角色，让第一个动作或第一句对白自然发生。'
    scrubbed=VOICE_NEGATED.sub('',raw.replace(generic,'交代场景和主要角色。'))
    if VOICE.search(scrubbed):
        return False,'含对白、演唱、歌词或可能需要保留的人声音轨'
    if mode=='continuous':
        return True,'已指定同一首伴奏，未发现需保护的人声指令'
    if MUSICAL.search(scrubbed):
        return True,'创意明确为舞蹈或器乐，且没有指定人声'
    return False,'创意未明确要求舞蹈或器乐，保留原声'

def _db(x):return float(20*np.log10(max(1e-12,float(x))))
def _rms(x):return float(np.sqrt(np.mean(np.asarray(x,dtype=np.float64)**2))) if np.size(x) else 0.
def _decode(path,seconds):
    cmd=['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-i',str(path),'-map','0:a:0','-t',f'{seconds:.6f}','-ac','2','-ar',str(SR),'-f','f32le','pipe:1']
    res=subprocess.run(cmd,capture_output=True,timeout=max(60,seconds*4))
    if res.returncode:raise ValueError('第一镜没有可读取的音频：'+res.stderr.decode(errors='replace')[-260:])
    x=np.frombuffer(res.stdout,dtype='<f4').reshape(-1,2)
    if len(x)<2.5*SR or not np.isfinite(x).all():raise ValueError('第一镜有效音频不足 2.5 秒或存在无效采样')
    if _db(_rms(x)) < -55:raise ValueError('第一镜音轨接近静音，不能据此延续音乐')
    return x

def _features(x):
    # Aggregate *power* across both channels; anti-phase material must not vanish in a mono fold.
    y=x[:(len(x)//4)*4].reshape(-1,4,2).mean(axis=1)
    w=720
    fr=np.lib.stride_tricks.sliding_window_view(y,w,axis=0)[::HOP//4]
    fr=fr*np.hanning(w)[None,None,:]
    spec=(np.abs(np.fft.rfft(fr,axis=-1))**2).mean(axis=1)
    f=np.fft.rfftfreq(w,1/(SR/4));edges=[35,65,100,160,250,400,700,1100,1700,2600,3900,5700]
    power=np.stack([spec[:,(f>=lo)&(f<hi)].mean(1) for lo,hi in zip(edges,edges[1:])],axis=1)
    logp=np.log(power+max(1e-10,float(np.percentile(power,85))*1e-5))
    norm=(logp-np.median(logp,axis=0))/np.maximum(.65,np.std(logp,axis=0))
    flux=np.maximum(0,np.diff(norm,axis=0,prepend=norm[:1])).mean(1)
    flux=flux-np.convolve(flux,np.ones(33)/33,mode='same')
    return norm,flux

def _tempo(onset):
    fps=SR/HOP;env=np.asarray(onset)
    def corr(lag):
        if not 0<lag<len(env)-8:return 0.
        a,b=env[lag:],env[:-lag]
        return float(np.dot(a,b)/(np.linalg.norm(a)*np.linalg.norm(b)+1e-12))
    lags=np.arange(30,96)
    scores=np.array([.63*corr(int(k))+.27*corr(2*int(k))+.10*corr(3*int(k)) for k in lags])
    # Very small prior breaks half/double-tempo ties without creating periodicity.
    ranks=scores+.014*np.exp(-.5*((60*fps/lags-120)/28)**2)
    i=int(ranks.argmax());lag=float(lags[i]);confidence=float(scores[i])
    if 0<i<len(scores)-1:
        a,b,c=scores[i-1:i+2];den=a-2*b+c
        if den<-.001:lag+=float(np.clip(.5*(a-c)/den,-.45,.45))
    reliable=confidence>=.19 and confidence-float(np.median(scores))>.095
    return (60*fps/lag if reliable else None),confidence

def _loop(x):
    features,onset=_features(x);n=len(features);bpm,conf=_tempo(onset)
    if n<210:raise ValueError('第一镜中没有足够的音乐用于选择循环')
    beat=6000/bpm if bpm else 52.
    fade=int(round(SR*max(.040,min(.100,beat/100*.14))))
    # Avoid silent intro/outro material as repeat anchors. Time alignment matches windows on both sides.
    energy=np.sqrt(np.maximum(0,np.mean(np.asarray(x[:(len(x)//HOP)*HOP]).reshape(-1,HOP*2)**2,axis=1)))
    threshold=max(.0007,float(np.percentile(energy,70))*.22)
    offsets=np.arange(-12,14,2)
    best=None
    if bpm:
        loops=[(b,round(b*beat)) for b in (16,8,4) if b*beat<n-37 and b*beat>100]
        if not loops:loops=[(2,round(beat*2))]
    else:
        loops=[(None,L) for L in range(max(105,int(.43*n)),int(.87*n),max(4,n//80))]
    for beats,nominal in loops:
        jitter=min(7,max(2,round(.011*nominal))) if beats else 1
        for length in range(nominal-jitter,nominal+jitter+1):
            starts=np.arange(16,n-length-15,2)
            if not len(starts):continue
            ends=starts+length
            ok=(energy[starts]>threshold)&(energy[ends]>threshold)
            if not ok.any():continue
            a,b=starts[ok],ends[ok]
            before=a[:,None]+offsets;after=b[:,None]+offsets
            spect=np.mean(np.abs(features[before]-features[after]),axis=(1,2))
            attacks=np.mean(np.abs(onset[before]-onset[after]),axis=1)
            ed=np.abs(np.log(np.maximum(energy[a],1e-7)/np.maximum(energy[b],1e-7)))
            # Prefer longer phrases, retaining more of shot 1 before the first join.
            cost=spect+.27*attacks+.11*ed+.32*(1.-length/n)+.13*(1.-b/n)
            if beats:cost+=.1*abs(length-nominal)/max(1,jitter)
            q=int(np.argmin(cost));entry=(float(cost[q]),int(a[q]),int(b[q]),beats)
            if best is None or entry[0]<best[0]:best=entry
    if best is None:raise ValueError('无法在第一镜找到两个非静音音乐接点；保留原声更合适')
    cost,af,bf,beats=best
    # FFT feature frames are centred 30 ms after their timestamp.
    start=af*HOP+1440;end=bf*HOP+1440
    # Sub-frame refinement on the louder channel reduces hollow stereo summing within crossfades.
    ch=int(np.mean(x[start-fade:start]**2,axis=0).argmax());ref=x[start-fade:start,ch].copy();ref-=ref.mean()
    radius=round(.009*SR);src=x[end-fade-radius:end+radius,ch].copy()
    scores=np.correlate(src,ref,'valid')
    e=np.r_[0.,np.cumsum(src.astype('float64')**2)]
    normalized=scores/(np.sqrt(np.maximum(1e-10,e[fade:]-e[:-fade]))*np.linalg.norm(ref)+1e-10)
    delta=int(np.argmax(normalized))-radius;end+=delta
    a=x[end-fade:end].astype('float64');b=x[start-fade:start].astype('float64')
    rho=float(np.clip(np.sum(a*b)/(np.linalg.norm(a)*np.linalg.norm(b)+1e-12),-.95,1.))
    return start,end,fade,rho,{'method':'beat_phrase' if bpm else 'texture_fallback','estimated_bpm':round(bpm,2) if bpm else None,'tempo_periodicity':round(conf,3),'phrase_beats':beats,'anchor_spectral_cost':round(cost,3),'alignment_shift_ms':round(delta*1000/SR,2),'warning':None if bpm else '没有可靠节拍，改按音色和能量选择原曲内循环；没有推断 BPM。'}

def h3_continuous_music(pieces,durations,destination):
    """Render only WAV. Caller may replace a video's audio with this; never edits the source clips.
    No synthesis, time stretch, other shot audio, or network access. durations are seconds.
    Raises ValueError/RuntimeError if the first bed cannot be used; caller should report fallback.
    """
    pieces=list(pieces);dur=[float(t) for t in durations];dest=Path(destination)
    if not pieces or len(pieces)!=len(dur) or any(not math.isfinite(t) or t<=0 for t in dur) or sum(dur)>900:raise ValueError('镜头时长无效或总长超过 900 秒')
    if dest.suffix.lower()!='.wav':raise ValueError('目标必须是 WAV')
    if any(Path(p).resolve()==dest.resolve() for p in pieces):raise ValueError('不可覆盖源镜头')
    if any(not Path(p).is_file() for p in pieces):raise ValueError('源镜头文件不存在')
    x=_decode(pieces[0],dur[0]);tgt=round(sum(dur)*SR)
    if len(pieces)==1 and len(x)>=tgt-2:
        audio=np.pad(x,((0,max(0,tgt-len(x))),(0,0)))[:tgt].copy()
        details={'method':'original_single_shot','estimated_bpm':None,'warning':None};joins=[];fade=0
    else:
        start,end,fade,rho,details=_loop(x)
        w=(.5-.5*np.cos(np.linspace(0,np.pi,fade)))[:,None]
        # Correlation-aware equal-power/constant-amplitude blend; first and last samples are untouched.
        scale=np.minimum(1.45,1/np.sqrt(np.maximum(.2,(1-w)**2+w**2+2*rho*w*(1-w))))
        cross=(scale*((1-w)*x[end-fade:end]+w*x[start-fade:start])).astype(np.float32)
        loop=np.concatenate([x[start:end-fade],cross])
        audio=np.empty((tgt,2),dtype=np.float32);intro=np.concatenate([x[:end-fade],cross])
        p=min(tgt,len(intro));audio[:p]=intro[:p];joins=[]
        while p<tgt:
            joins.append(p)
            take=min(len(loop),tgt-p);audio[p:p+take]=loop[:take];p+=take
        details.update(loop_start_s=round(start/SR,4),loop_end_s=round(end/SR,4),loop_s=round((end-start)/SR,4),fade_ms=round(fade*1000/SR,1),fade_correlation=round(rho,3))
    tail=min(len(audio)//4,int(SR*.18));head=min(len(audio)//4,int(SR*.008))
    audio[:head]*=np.linspace(0,1,head)[:,None];audio[-tail:]*=np.linspace(1,0,tail)[:,None]
    peak=float(np.max(np.abs(audio)));gain=min(1.,.965/max(peak,1e-12));audio*=gain
    # Objective continuity measures, not claims about melody or music recognition.
    metrics=[]
    for p in joins:
        if p<fade+480 or p+480>=len(audio):continue
        jump=float(np.max(np.abs(audio[p]-audio[p-1])))
        env_step=abs(_db(_rms(audio[p-480:p]))-_db(_rms(audio[p:p+480])))
        metrics.append({'at_s':round(p/SR,4),'step_dbfs':round(_db(jump),2),'rms_delta_10ms_db':round(env_step,2)})
    dest.parent.mkdir(parents=True,exist_ok=True);tmp=None
    try:
        with tempfile.NamedTemporaryFile(suffix='.wav',dir=dest.parent,delete=False) as f:tmp=f.name
        with wave.open(tmp,'wb') as out:
            out.setnchannels(2);out.setsampwidth(2);out.setframerate(SR)
            for offset in range(0,len(audio),SR*5):
                out.writeframes(np.rint(np.clip(audio[offset:offset+SR*5],-1,1)*32767).astype('<i2').tobytes())
        os.replace(tmp,dest)
    finally:
        if tmp:Path(tmp).unlink(missing_ok=True)
    return {**details,'source':'first_shot_only','wav':str(dest),'seconds':round(tgt/SR,6),'sample_rate':SR,'channels':2,'joins':metrics,'peak_dbfs':round(_db(peak*gain),2),'rms_dbfs':round(_db(_rms(audio)),2)}

#@title 连接网页工作台 · A100/H100/G4
# 在可用的 Colab 会话中启动；不公开虚拟机端口，也不续命。
from pathlib import Path
import os, re, time, json, uuid, hashlib, threading, shutil, copy, subprocess, sys
import requests
import fcntl
_managed_lock=open('/content/h3-managed.lock','a')
fcntl.flock(_managed_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
def exchange(name):
    return next((p/name for p in (Path('/content'),Path('/'),Path.cwd()) if (p/name).is_file()),None)
config=json.loads(exchange('h3managedidentity.json').read_text())
token=config['worker_key'];did=config['worker_id'];BASE='https://withkeya.com/h3-studio'
gpu_present=subprocess.check_output(['nvidia-smi','--query-gpu=name','--format=csv,noheader'],text=True).strip()
if not re.search(r'\b(?:[AH]100|G4|RTX\s+PRO\s+6000\s+Blackwell)\b',gpu_present): raise RuntimeError('unsupported GPU')
ROOT=root=Path('/content/h3-managed-data');root.mkdir(exist_ok=True)
models, videos=root/'models', root/'videos'
mounted=Path('/content/drive/MyDrive/MiniMax-H3/models')
if mounted.is_dir():models=mounted
for d in (models,videos):d.mkdir(parents=True,exist_ok=True)
PRECISION=globals().get('H3_STUDIO_MODEL','quantized')
if PRECISION not in ('quantized','bf16'):raise ValueError('未知执行端类型')
if globals().get('_h3_worker') and _h3_worker.is_alive():
    raise RuntimeError('此会话已有执行端，请使用独立运行时。')
if PRECISION=='bf16':
    import h3_bf16
    bf16_runtime=h3_bf16.prepare()
else:
    from huggingface_hub import hf_hub_download
    files = {'diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors': 20970379616, 'text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors': 15687142551, 'vae/minimax_h3_audio_vae_fp32.safetensors': 605254808, 'vae/minimax_h3_video_vae_int8_convrot.safetensors': 2811065184}
    def prepare_model(pair):
        name, size = pair
        path = models / name
        if path.is_file() and path.stat().st_size == size:
            print('复用：', name)
            return
        try:
            hf_hub_download(repo_id='Comfy-Org/MiniMax-H3', revision='54472221d27c695fca9621b0c3ed52f06c4af707', filename=name, local_dir=str(models), force_download=path.exists(), token=False)
            assert path.stat().st_size == size, name
        except Exception:
            if not restore_cached_model(path, name, BASE, token, did, gpu_present):
                raise
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as downloads:
        list(downloads.map(prepare_model, files.items()))
    print('四个文件已就绪')
    import time, requests
    repo = Path('/content/MiniMax-H3-ComfyUI')
    base='http://127.0.0.1:8192'
    try: warm = requests.get(base+'/system_stats',timeout=2).ok
    except requests.RequestException: warm=False
    if not repo.exists():
        subprocess.run(['git', 'clone', '--filter=blob:none', 'https://github.com/Comfy-Org/ComfyUI.git', str(repo)], check=True)
    if not warm:
        subprocess.run(['git', '-C', str(repo), 'checkout', '1332fabfcc507aa7c548cd5152071ed494c207e8'], check=True)
        subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', '-r', str(repo/'requirements.txt')], check=True)
    for name in files:
        link = repo/'models'/name
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.is_symlink() and not link.exists():
            link.symlink_to(models/name)
    base = 'http://127.0.0.1:8192'
    log = Path('/content/minimax-h3-comfy.log')
    if not warm:
        with log.open('w') as stream:
            server = subprocess.Popen([sys.executable, 'main.py', '--listen', '127.0.0.1', '--port', '8192', '--disable-auto-launch'], cwd=repo, stdout=stream, stderr=subprocess.STDOUT)
    for attempt in range(240):
        if not warm and server.poll() is not None:
            raise RuntimeError(log.read_text(errors='replace')[-3500:])
        try:
            if requests.get(base+'/system_stats', timeout=3).ok: break
        except requests.ConnectionError: pass
        time.sleep(1)
    else: raise RuntimeError('服务尚未就绪，请查看 '+str(log))
    print('准备完成')
    graph={'92': {'inputs': {'filename_prefix': 'video/MiniMax_H3', 'format': 'mp4', 'codec': 'h264', 'video': ['140:130', 0]}, 'class_type': 'SaveVideo', '_meta': {'title': 'Save Video'}}, '140:119': {'inputs': {'vae_name': 'minimax_h3_video_vae_int8_convrot.safetensors'}, 'class_type': 'VAELoader', '_meta': {'title': 'Load VAE'}}, '140:120': {'inputs': {'vae_name': 'minimax_h3_audio_vae_fp32.safetensors'}, 'class_type': 'VAELoader', '_meta': {'title': 'Load VAE'}}, '140:121': {'inputs': {'samples': ['140:125', 0], 'vae': ['140:120', 0]}, 'class_type': 'VAEDecodeAudio', '_meta': {'title': 'VAE Decode Audio'}}, '140:122': {'inputs': {'samples': ['140:125', 0], 'vae': ['140:119', 0]}, 'class_type': 'VAEDecode', '_meta': {'title': 'VAE Decode'}}, '140:123': {'inputs': {'sampler_name': 'res_multistep'}, 'class_type': 'KSamplerSelect', '_meta': {'title': 'KSamplerSelect'}}, '140:124': {'inputs': {'scheduler': 'simple', 'steps': 20, 'denoise': 1, 'model': ['140:127', 0]}, 'class_type': 'BasicScheduler', '_meta': {'title': 'BasicScheduler'}}, '140:125': {'inputs': {'noise': ['140:129', 0], 'guider': ['140:126', 0], 'sampler': ['140:123', 0], 'sigmas': ['140:124', 0], 'latent_image': ['140:131', 1]}, 'class_type': 'SamplerCustomAdvanced', '_meta': {'title': 'SamplerCustomAdvanced'}}, '140:126': {'inputs': {'model': ['140:127', 0], 'conditioning': ['140:131', 0]}, 'class_type': 'BasicGuider', '_meta': {'title': 'Basic Guider'}}, '140:127': {'inputs': {'unet_name': 'minimax_h3_fl2va_pruned_int8_convrot.safetensors', 'weight_dtype': 'default'}, 'class_type': 'UNETLoader', '_meta': {'title': 'Load Diffusion Model'}}, '140:128': {'inputs': {'clip_name': 'qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors', 'type': 'minimax', 'device': 'default'}, 'class_type': 'CLIPLoader', '_meta': {'title': 'Load CLIP'}}, '140:129': {'inputs': {'noise_seed': 757358688076805}, 'class_type': 'RandomNoise', '_meta': {'title': 'RandomNoise'}}, '140:130': {'inputs': {'fps': 24, 'bit_depth': 8, 'color_space': 'sRGB', 'codec': 'none', 'images': ['140:122', 0], 'audio': ['140:121', 0]}, 'class_type': 'CreateVideo', '_meta': {'title': 'Create Video'}}, '140:131': {'inputs': {'prompt': '', 'width': 768, 'height': 448, 'length': 73, 'clip': ['140:128', 0], 'vae': ['140:119', 0]}, 'class_type': 'MiniMaxH3ImageToVideo', '_meta': {'title': 'MiniMax H3 Image to Video'}}}
# 雨落，火开｜三个连续镜头，24 fps，成片 30 秒

import math
import logging
gpu_name = subprocess.check_output(['nvidia-smi','--query-gpu=name','--format=csv,noheader'],text=True).strip()
worker_id=config.get('worker_id','myles-colab-h3')
ACTIVE_DIR=root/'studio-jobs'; ACTIVE_DIR.mkdir(exist_ok=True)
runtime_id=hashlib.sha256(subprocess.check_output(['nvidia-smi','--query-gpu=uuid','--format=csv,noheader'])).hexdigest()[:24]
def rpc(route, obj):
    r=requests.post(BASE+route,headers={'Authorization':'Bearer '+token},json={'worker_id':worker_id,'gpu':gpu_name,'runtime_id':runtime_id,'precision':PRECISION,'shot_v1':True,'relay_v1':True,'append_v1':True,'turbo8':PRECISION=='quantized','finish_v2':True,**obj},timeout=(8,25),allow_redirects=False)
    r.raise_for_status()
    return r.json() if r.content else {}
def durable(p,data):
    tmp=p.with_name(p.name+'.part');tmp.write_text(json.dumps(data,ensure_ascii=False));tmp.replace(p)
def ff(*args):
    return subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y',*map(str,args)],check=True,capture_output=True)
def drive_result(p,ident):
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
def source_for(job,path):
    src=path/'original.mp4'
    if not src.exists():
        tmp=src.with_suffix('.part')
        with requests.post(BASE+'/api/worker/source',headers={'Authorization':'Bearer '+token},json={'id':job['id'],'lease_token':job['lease_token'],'worker_id':worker_id,'runtime_id':runtime_id,'gpu':gpu_name},timeout=(12,180),stream=True,allow_redirects=False) as r:
            r.raise_for_status()
            with tmp.open('wb') as f:
                for block in r.iter_content(262144):f.write(block)
        tmp.replace(src)
    meta=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=width,height','-of','json',src]))['streams'][0]
    job['original_geometry']=(int(meta['width']),int(meta['height']))
    last=path/'original-last.png'
    ff('-sseof','-2','-i',src,'-an','-update','1',last)
    return src,last
def append_video(src,tail,dest,geometry):
    w,h=geometry
    vf=f'scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,format=yuv420p'
    af='aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS'
    graph=f'[0:v]{vf},setpts=PTS-STARTPTS[v0];[0:a]{af}[a0];[1:v]{vf},setpts=PTS-STARTPTS[v1];[1:a]{af}[a1];[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]'
    ff('-i',src,'-i',tail,'-filter_complex',graph,'-map','[v]','-map','[a]','-c:v','libx264','-preset','fast','-crf','16','-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-movflags','+faststart',dest)
def dimensions(job):
    is_hd=job.get('quality')=='hd'
    def orientation(txt):
        modes=re.findall(r'竖[屏版幅]|纵向构图|9[：:]16|portrait orientation|vertical video|横[屏版幅]|横向构图|16[：:]9|landscape orientation|正方形|1[：:]1',txt,re.I)
        if not modes:return None
        m=modes[-1]
        return 'portrait' if re.search(r'竖|纵|^9|portrait|vertical',m,re.I) else 'square' if re.search(r'正方|^1[：:]1$',m) else 'landscape'
    orient=orientation(job.get('brief','')) or orientation(job['prompt']) or orientation(' '.join(s['prompt'] for s in job['shots'])) or 'landscape'
    long,short=(1344,768) if is_hd else (768,448)
    if job.get('original_geometry'):
        w,h=job['original_geometry'];orient='portrait' if h>w else ('square' if h==w else 'landscape')
    width,height=(short,long) if orient=='portrait' else (short,short) if orient=='square' else (long,short)
    return width,height

def checkpoint_piece(job,ix,piece,upload=False):
    url=BASE+'/api/worker/media/checkpoint/'+job['id']+'/'+str(ix)
    headers={**relay_headers(),'X-H3-Lease':job.get('lease_token',''),'X-H3-Shot-Ticket':job.get('_shot_ticket','')}
    if upload:
        meta=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',piece]))
        video=next(x for x in meta['streams'] if x['codec_type']=='video')
        if (video['width'],video['height'])!=dimensions(job) or float(meta['format']['duration'])<float(job['shots'][ix]['seconds'])-.15:
            raise ValueError('本机镜头尺寸或时长不完整，已保留文件')
        sha=file_digest(piece);headers.update({'Content-Type':'video/mp4','X-Content-SHA256':sha})
        for attempt in range(3):
            with piece.open('rb') as stream:
                r=requests.put(url,headers=headers,data=stream,timeout=(10,90),allow_redirects=False)
            if r.status_code not in (429,502,503,504) or attempt==2:break
            time.sleep(2*(attempt+1))
        r.raise_for_status();return True
    tmp=piece.with_suffix('.restore.part')
    try:
        with requests.get(url,headers=headers,stream=True,timeout=(10,90),allow_redirects=False) as r:
            if r.status_code==404:return False
            r.raise_for_status();sha=r.headers.get('X-Content-SHA256','');size=int(r.headers['Content-Length'])
            with tmp.open('wb') as f:
                for chunk in r.iter_content(262144):f.write(chunk)
                f.flush();os.fsync(f.fileno())
        if tmp.stat().st_size!=size or file_digest(tmp)!=sha:raise ValueError('服务器镜头校验失败，停止重算')
        tmp.replace(piece);durable(piece.with_suffix('.checkpoint.json'),{'spec':job['checkpoint_spec']})
        return True
    finally:tmp.unlink(missing_ok=True)

def restore_pieces(job,pieces,update):
    pending=set()
    for ix,piece in enumerate(pieces):
        if checkpoint_piece(job,ix,piece):
            update('running',int(4+85*(ix+1)/len(pieces)),f'已恢复第 {ix+1} 镜，无需重算');continue
        if piece.is_file():
            marker=piece.with_suffix('.checkpoint.json')
            if not marker.is_file() or json.loads(marker.read_text()).get('spec')!=job['checkpoint_spec']:
                raise ValueError('发现旧本机片段，已保留；需要核对渲染计划后恢复')
            checkpoint_piece(job,ix,piece,True)
        else:pending.add(ix)
    return pending

def render_piece(job,ix,path,piece,previous,update):
    if checkpoint_piece(job,ix,piece):return
    durable(piece.with_suffix('.checkpoint.json'),{'spec':job['checkpoint_spec']})
    _render_piece(job,ix,path,piece,previous,update)
    checkpoint_piece(job,ix,piece,True)

def _render_piece(job,ix,path,piece,previous,update):
    ident=job['id'];s=job['shots'][ix];d=float(s['seconds'])
    width,height=dimensions(job);pct=int(4+85*ix/len(job['shots']))
    if (job.get('precision') or 'quantized')!=PRECISION:raise ValueError('任务与执行端模型不符')
    if PRECISION=='bf16':
        seed=int.from_bytes(hashlib.sha256((ident+str(ix)).encode()).digest()[:6],'big')
        return h3_bf16.render(bf16_runtime,job,ix,piece,previous,update,(width,height),seed)
    g=copy.deepcopy(graph);fr=5+17*max(0,math.ceil((d*24-5)/17))
    g['140:131']['inputs'].update(prompt=s.get('h3_prompt') or (str(job['prompt'])+'。本镜头：'+str(s['prompt'])),length=fr,width=width,height=height)
    g['140:129']['inputs']['noise_seed']=int.from_bytes(hashlib.sha256((ident+str(ix)).encode()).digest()[:6],'big')
    g['92']['inputs']['filename_prefix']='video/studio-'+path.name+'-'+piece.stem
    if job.get('quality')=='fast':
        fname='minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors'
        link=repo/'models'/'loras'/fname; link.parent.mkdir(parents=True,exist_ok=True)
        cached=models/'loras'/fname
        if not cached.exists() or cached.stat().st_size!=1956193000:
            update('running',pct,'首次准备快速预览组件（约 1.96 GB）')
            hf_hub_download(repo_id='lightx2v/Minimax-h3-Turbo',revision='30f485ed874aa44c8183348ad361c5eabad318da',filename=fname,local_dir=str(cached.parent),force_download=cached.exists(),token=False)
        if not link.exists():link.symlink_to(cached)
        g['turbo']={'class_type':'LoraLoaderModelOnly','inputs':{'model':['140:127',0],'strength_model':1.0,'lora_name':fname}}
        g['140:124']['inputs'].update(model=['turbo',0],steps=8)
        g['140:126']['inputs']['model']=['turbo',0]
    if previous is not None:
        im='studio-'+path.name+'-'+str(ix)+'.png'
        if previous.suffix=='.png':shutil.copyfile(previous,repo/'input'/im)
        else:ff('-sseof','-0.18','-i',previous,'-frames:v','1',repo/'input'/im)
        g['init']={'class_type':'LoadImage','inputs':{'image':im}}
        g['140:131']['inputs']['first_frame']=['init',0]
    # Reattach to an already submitted Comfy job if the worker thread restarted in the same VM.
    track=piece.with_suffix('.queue.json') if piece.stem.startswith('helper-') else path/(str(ix)+'-queue.json')
    cid=None
    if track.exists():
        saved=json.loads(track.read_text()).get('id')
        hist=requests.get(base+'/history/'+saved,timeout=10).json()
        queue=requests.get(base+'/queue',timeout=10).json()
        if saved in hist or any(el[1]==saved for el in queue.get('queue_running',[])+queue.get('queue_pending',[])):cid=saved
    if not cid:
        response=requests.post(base+'/prompt',json={'prompt':g},timeout=30);response.raise_for_status()
        cid=response.json()['prompt_id'];durable(track,{'id':cid})
    last=0
    while True:
        if time.monotonic()-last>=25:
            update('running',pct,f"生成第 {ix+1}/{len(job['shots'])} 镜");last=time.monotonic()
        state=requests.get(base+'/history/'+cid,timeout=20).json().get(cid)
        if state:break
        time.sleep(4)
    if state.get('status',{}).get('status_str')=='error':raise RuntimeError('H3 生成失败，请查看运行时日志')
    file=next(m for arr in state['outputs']['92'].values() if isinstance(arr,list) for m in arr if isinstance(m,dict) and m.get('filename','').endswith('.mp4'))
    temp=piece.with_suffix('.part');shutil.copyfile(repo/'output'/file.get('subfolder','')/file['filename'],temp);temp.replace(piece)

def relay_headers():
    return {'Authorization':'Bearer '+token,'X-H3-Worker-ID':worker_id,'X-H3-GPU':gpu_name,'X-H3-Runtime':runtime_id}
def file_digest(path):
    with path.open('rb') as f:
        h=hashlib.sha256()
        for b in iter(lambda:f.read(262144),b''):h.update(b)
    return h.hexdigest()

def take_assisted_piece(job,ix,path,piece,update):
    # Only the owning runtime writes the canonical (00.mp4, 01.mp4, ...) paths.
    args={'id':job['id'],'ix':ix,'lease_token':job['lease_token']}
    reply=rpc('/api/worker/shot/status',args)
    if reply['state']=='local':return False
    if reply['state']=='ready':
        if reply.get('sha'):
            tmp=piece.with_suffix('.relay.part')
            try:
                with requests.get(BASE+'/api/worker/media/shot/'+reply['ticket'],headers={**relay_headers(),'X-H3-Lease':job['lease_token']},stream=True,timeout=(8,45),allow_redirects=False) as response:
                    response.raise_for_status()
                    with tmp.open('wb') as f:
                        for chunk in response.iter_content(262144):f.write(chunk)
                if tmp.stat().st_size==reply['size'] and file_digest(tmp)==reply['sha']:
                    tmp.replace(piece);return True
            except (OSError,requests.RequestException):pass
            finally:tmp.unlink(missing_ok=True)
            return False
        src=path/('helper-'+reply['ticket']+'.mp4')
        # Drive visibility can lag behind the server acknowledgement.
        for _ in range(8):
            try:
                if src.stat().st_size==reply['size']:
                    tmp=piece.with_suffix('.part')
                    shutil.copyfile(src,tmp)
                    if tmp.stat().st_size==reply['size']:
                        tmp.replace(piece)
                        try:src.unlink()
                        except OSError:pass
                        return True
            except OSError:pass
            time.sleep(2)
        return False
    return None  # helper is still rendering; look for another ready shot

def transient_error(exc):
    if isinstance(exc,(requests.ConnectionError,requests.Timeout)):return True
    return isinstance(exc,requests.HTTPError) and exc.response is not None and (exc.response.status_code==429 or exc.response.status_code>=500)

def render_assist(task):
    job=task['job'];job['_shot_ticket']=task['ticket'];ix=task['ix'];ticket=task['ticket']
    path=ACTIVE_DIR/hashlib.sha256(job['id'].encode()).hexdigest()[:24];path.mkdir(exist_ok=True)
    piece=path/('helper-'+ticket+'.mp4')
    args={'id':job['id'],'ix':ix,'ticket':ticket}
    def pulse(*_):rpc('/api/worker/shot/pulse',args)
    try:
        pulse()
        render_piece(job,ix,path,piece,None,pulse)
        if task.get('relay'):
            pulse()
            with piece.open('rb') as f:
                r=requests.put(BASE+'/api/worker/media/shot/'+ticket,data=f,headers={**relay_headers(),'Content-Type':'video/mp4','X-Content-SHA256':file_digest(piece)},timeout=(8,60),allow_redirects=False);r.raise_for_status()
        rpc('/api/worker/shot/ready',{**args,'size':piece.stat().st_size})
        if task.get('relay'):
            piece.unlink(missing_ok=True);piece.with_suffix('.queue.json').unlink(missing_ok=True)
            try:path.rmdir()
            except OSError:pass
    except Exception as exc:
        if transient_error(exc):raise
        try:rpc('/api/worker/shot/release',args)
        except Exception:pass
        raise

def render_job(job):
    import shutil as _shutil
    ident=str(job['id']); lease=job['lease_token']
    shots=job['shots']
    if not 1<=len(shots)<=12:raise ValueError('无效分镜数')
    durations=[float(s['seconds']) for s in shots]
    if any(not (0.5<=d<=15) for d in durations) or abs(sum(durations)-float(job['seconds']))>0.1:raise ValueError('分镜时长无效')
    path=ACTIVE_DIR/hashlib.sha256(ident.encode()).hexdigest()[:24]
    final=videos/('studio-'+path.name+'.mp4')
    def _cleanup_scratch():
        if path.is_dir() and final.parent.resolve()!=path.resolve():_shutil.rmtree(path,ignore_errors=True)
    def update(state,progress,message,url=None):
        body={'id':ident,'lease_token':lease,'state':state,'progress':progress,'message':message}
        if url:body['result_url']=url
        return rpc('/api/worker/update',body)
    update('running',1,'正在准备 GPU')
    if final.is_file() and final.stat().st_size>0:
        url=drive_result(final,ident);update('done',100,'已从 Drive 恢复成片',url);_cleanup_scratch();return
    path.mkdir(exist_ok=True)
    src,last=source_for(job,path) if job.get('append_source') else (None,None)
    pieces=[path/f'{i:02d}.mp4' for i in range(len(shots))]
    pending=restore_pieces(job,pieces,update)
    width,height=dimensions(job)
    is_hd=job.get('quality')=='hd'
    while pending:
        pct=int(4+85*(len(shots)-len(pending))/len(shots))
        for ix in sorted(pending):
            cut=ix>0 and shots[ix].get('cut') is True and job.get('route')!='continuous'
            if not cut and ix-1 in pending:continue
            assisted=False
            if cut and job.get('parallel_cuts'):
                assisted=take_assisted_piece(job,ix,path,pieces[ix],update)
                if assisted is None:continue
            if not assisted:
                render_piece(job,ix,path,pieces[ix],last if not ix else (None if cut else pieces[ix-1]),
                             lambda state,_graph_progress,msg:update(state,pct,msg))
            pending.remove(ix)
            break
        else:
            update('running',pct,'等待独立分镜交回')
            time.sleep(5)
    update('running',92,'拼接视频，校正音轨响度')
    cmd=[];filters=[]
    for i,(v,d) in enumerate(zip(pieces,durations)):
        cmd+=['-i',v]
        filters.append(f'[{i}:v]trim=duration={d},setpts=PTS-STARTPTS[v{i}];[{i}:a]aresample=48000,apad,atrim=duration={d},asetpts=PTS-STARTPTS[a{i}]')
    use_music,why=h3_music_policy(job['prompt'],job.get('audio_mode','original'),[sh['prompt'] for sh in shots])
    music_ok=False
    if len(pieces)>1 and use_music:
        try:
            score=path/'continuous-score.wav'
            report=h3_continuous_music(pieces,durations,score)
            durable(path/'music-report.json',report)
            music_ok=True
        except ValueError as exc:
            durable(path/'music-report.json',{'fallback':'original','reason':str(exc)[:240]})
    if music_ok:
        cmd+=['-i',score]
        filters=[f'[{i}:v]trim=duration={d},setpts=PTS-STARTPTS[v{i}]' for i,d in enumerate(durations)]
        filters.append(''.join(f'[v{i}]' for i in range(len(pieces)))+f'concat=n={len(pieces)}:v=1:a=0[v];[{len(pieces)}:a]loudnorm=I=-16:TP=-1.5:LRA=8,aresample=48000[a]')
    else:
        inputs=''.join(f'[v{i}][a{i}]' for i in range(len(pieces)))
        filters.append(inputs+f'concat=n={len(pieces)}:v=1:a=1[v][raw];[raw]loudnorm=I=-16:TP=-1.5:LRA=8,aresample=48000[a]')
    tmp=path/'delivery.tmp.mp4';ff(*cmd,'-filter_complex',';'.join(filters),'-map','[v]','-map','[a]','-c:v','libx264','-preset','fast','-crf','15' if is_hd else '17','-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-t',sum(durations),'-movflags','+faststart',tmp)
    if src:
        joined=path/'joined.tmp.mp4';append_video(src,tmp,joined,job['original_geometry']);joined.replace(final)
    else:tmp.replace(final)
    url=drive_result(final,ident)
    note='配乐连续' if music_ok else ('保留原声（首段音乐不足）' if use_music and len(pieces)>1 else '原声')
    update('done',100,f'已保存 · {width}×{height} · '+note,url)
    _cleanup_scratch()
def worker_loop():
    resume_lease='';assist=None
    while True:
        job=None
        try:
            if assist:
                render_assist(assist);assist=None;continue
            claim=rpc('/api/worker/claim',{'resume_lease':resume_lease} if resume_lease else {})
            job=claim.get('job');assist=claim.get('assist')
            if assist:continue
            if job:
                resume_lease=job['lease_token']
                render_job(job);resume_lease=''
            else:
                resume_lease='';time.sleep(8)
        except Exception as exc:
            if transient_error(exc):
                time.sleep(8);continue
            if job:
                # Terminal render errors fail this job; stale leases cannot affect its new owner.
                try:rpc('/api/worker/update',{'id':job['id'],'lease_token':job['lease_token'],'state':'failed','progress':0,'message':'生成未完成：'+type(exc).__name__})
                except Exception:pass
            resume_lease='';assist=None;time.sleep(15)
if not globals().get('_h3_worker') or not _h3_worker.is_alive():
    rpc('/api/worker/heartbeat',{})
    _h3_worker=threading.Thread(target=worker_loop,daemon=True);_h3_worker.start()
def h3_sampling_snapshot(queue,active_dir,logfile):
    import json,re
    from pathlib import Path
    running=queue.get('queue_running') or []
    if len(running)!=1 or queue.get('queue_pending'):return
    _,cid,graph,*_=running[0]
    m=re.fullmatch(r'video/studio-([a-f0-9]{24})-(\d{2}|helper-[a-f0-9]{32})',graph['92']['inputs']['filename_prefix'])
    if not m:return
    tag,part=m.groups();folder=Path(active_dir)/tag
    track=folder/(part+'.queue.json' if part.startswith('helper-') else str(int(part))+'-queue.json')
    if json.loads(track.read_text()).get('id')!=cid:return
    n=int(graph['140:124']['inputs']['steps'])
    with Path(logfile).open('rb') as f:
        f.seek(0,2);f.seek(max(0,f.tell()-65536));tail=f.read().decode(errors='replace')
    segment=tail.rsplit('got prompt',1)[-1]
    if 'Prompt executed in' in segment or 'Exception during processing' in segment:return
    # A fresh graph may be loading. Never attribute a previous prompt's bar to it.
    if Path(logfile).stat().st_mtime+1<track.stat().st_mtime:return
    matches=list(re.finditer(r'\b(\d+)/(\d+)\s*\[([\d:]+)<[^\]\r\n]*?[, ]\s*([\d.]+)\s*(s/it|it/s)\]',segment))
    hits=[v for v in matches if int(v[2])==n and 0<int(v[1])<=n]
    if not hits:return
    v=hits[-1];rate=float(v[4])
    if v[5]=='it/s':rate=1/rate if rate else 0
    return dict(tag=tag,part=part,cid=cid,done=int(v[1]),total=n,rate=round(rate,2))

def h3_sampling_loop(send,active_dir,base):
    import fcntl,time,requests
    # One observer per VM, including when attached to an already-running worker.
    with open('/content/h3-sampler-observer.lock','a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        last=None;sent=0
        while True:
            try:
                queue=requests.get(base+'/queue',timeout=4).json()
                v=h3_sampling_snapshot(queue,active_dir,'/content/minimax-h3-comfy.log')
                if v and (v!=last or time.monotonic()-sent>28):
                    send('/api/worker/sampler',v);last=v;sent=time.monotonic()
            except Exception:pass  # telemetry must never affect rendering
            time.sleep(6)

if PRECISION=='quantized':threading.Thread(target=h3_sampling_loop,args=(rpc,ACTIVE_DIR,base),daemon=True).start()
print('工作台已连接：',BASE,'·',gpu_name,'·',PRECISION)

# H3_PRIVATE_PREVIEW_SYNC_V1
def _sync_private_previews_once():
    pending=rpc('/api/worker/media-pending',{}).get('jobs',[])
    if not pending:return
    by_id={}
    for p in videos.glob('*.mp4'):
        try:
            if p.is_file() and not p.is_symlink():
                by_id[os.getxattr(p,'user.drive.id').decode()]=p
        except OSError:continue
    for item in pending:
        jobid=str(item['id']); fid=str(item['drive_id'])
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,160}',jobid) or fid not in by_id:continue
        p=by_id[fid]
        with p.open('rb') as stream:
            r=requests.put(BASE+'/api/worker/media/'+jobid,params={'drive_id':fid},data=stream,
               headers={'Authorization':'Bearer '+token,'X-H3-Worker-ID':worker_id,'X-H3-GPU':gpu_name,'Content-Type':'video/mp4','Content-Length':str(p.stat().st_size)},
               timeout=(10,180),allow_redirects=False)
        if not r.ok:raise RuntimeError('同步预览 HTTP '+str(r.status_code))
def _studio_preview_loop():
    while True:
        try:
            _sync_private_previews_once()
            time.sleep(20)
        except Exception:
            time.sleep(30)
if not globals().get('_h3_preview_worker') or not _h3_preview_worker.is_alive():
    _h3_preview_worker=threading.Thread(target=_studio_preview_loop,daemon=True)
    _h3_preview_worker.start()

_h3_worker.join()
