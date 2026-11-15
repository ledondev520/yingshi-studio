"""Official FL2VA BF16 backend for one supported GPU with at least 80 GB. Invoked only by BF16 workers."""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REV = "42ed227ee7df40d41602854ae760620d6eb651fe"
DIFFUSERS = "84532a058702c74ae5d1dd6ceb05683fc5d5e768"
REPO = "MiniMaxAI/MiniMax-H3"
PARTS = {"transformer", "text_encoder", "vae", "audio_vae", "scheduler", "audio_scheduler", "tokenizer", "processor"}
FILES = {"LICENSE", "model_index.json", "modular_model_index.json"}


def prepare():
    import psutil
    from huggingface_hub import HfApi, snapshot_download
    info = subprocess.check_output(["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"], text=True)
    name, total, free = info.splitlines()[0].rsplit(",", 2)
    if not re.search(r"\b(?:[AH]100|G4|RTX\s+PRO\s+6000\s+Blackwell)\b", name) or int(total)<78000 or int(free)<73500:
        raise RuntimeError("BF16 需要一张空闲的 A100/H100 80 GB 或 G4 96 GB。")
    ram = psutil.virtual_memory().available
    for cap,used in (("/sys/fs/cgroup/memory.max","/sys/fs/cgroup/memory.current"),("/sys/fs/cgroup/memory/memory.limit_in_bytes","/sys/fs/cgroup/memory/memory.usage_in_bytes")):
        if Path(cap).exists() and Path(used).exists() and (v:=Path(cap).read_text().strip()).isdigit():
            stat=Path(used).with_name("memory.stat")
            fields=dict(line.split() for line in stat.read_text().splitlines()) if stat.exists() else {}
            reclaim=int(fields.get("inactive_file",fields.get("total_inactive_file",0)))
            ram = min(ram, int(v)-int(Path(used).read_text())+reclaim)
    if ram < 145*2**30: raise RuntimeError("BF16 工作端支持首帧续拍，请启用高 RAM（建议至少 145 GiB 可用）。")
    data = Path(os.environ.get("H3_BF16_MODEL_DIR","/content/drive/MyDrive/MiniMax-H3-BF16/models")) / REV
    data.mkdir(parents=True,exist_ok=True)
    manifest = {x.rfilename:x.size for x in HfApi().model_info(REPO,revision=REV,files_metadata=True).siblings
                if x.rfilename in FILES or x.rfilename.partition("/")[0] in PARTS}
    if not all(any(f.startswith(d+"/") for f in manifest) for d in PARTS) or not all(isinstance(v,int) for v in manifest.values()):
        raise ValueError("权重清单不完整")
    def pending(root):
        return [f for f,n in manifest.items() if not (root/f).is_file() or (root/f).stat().st_size!=n]
    missing = pending(data)
    if missing:
        if shutil.disk_usage(data).free < sum(manifest[f] for f in missing)+5_000_000_000: raise RuntimeError("Drive 空间不足")
        snapshot_download(REPO,revision=REV,allow_patterns=sorted(manifest),local_dir=str(data),max_workers=2)
    if pending(data): raise RuntimeError("Drive 权重不完整")
    scratch=Path("/mnt/local-scratch")
    src=data
    if scratch.is_mount() and scratch not in data.parents:
        src=scratch/("h3-bf16-"+REV[:7]);src.mkdir(exist_ok=True)
        missing=pending(src)
        if shutil.disk_usage(src).free < sum(manifest[f] for f in missing)+5_000_000_000: raise RuntimeError("本地暂存盘空间不足")
        def copy(f):
            dest=src/f;dest.parent.mkdir(parents=True,exist_ok=True)
            tmp=dest.with_name(dest.name+".part");shutil.copyfile(data/f,tmp)
            if tmp.stat().st_size!=manifest[f]: raise RuntimeError("暂存分片不完整")
            tmp.replace(dest)
        with ThreadPoolExecutor(max_workers=5) as pool:list(pool.map(copy,missing))
    venv=Path("/content/minimax-h3-bf16-env"); py=venv/"bin/python"
    if not py.exists():subprocess.run([sys.executable,"-m","venv","--without-pip","--system-site-packages",str(venv)],check=True)
    marker=venv/("studio-ready-"+DIFFUSERS)
    if not marker.is_file():
        subprocess.run([str(py),"-m","pip","install","--quiet","diffusers @ git+https://github.com/huggingface/diffusers.git@"+DIFFUSERS,"transformers==5.22.2","accelerate>=1.6","huggingface_hub","av","safetensors","sentencepiece"],check=True)
        subprocess.run([str(py),"-c","import av,torch,torchaudio; from diffusers import ComponentsManager,ModularPipeline; from diffusers.utils.export_utils import encode_video; assert torch.cuda.is_bf16_supported()"],check=True)
        marker.touch()
    return str(py),str(src)


def frame_plan(seconds):
    target=round(seconds*24)
    native=max(124,min(345,5+17*math.ceil((target-5)/17)))
    return native,target


def generate(cfg):
    import gc
    import torch
    from PIL import Image
    from diffusers import ComponentsManager,ModularPipeline
    from diffusers.utils.export_utils import encode_video
    weights=cfg["weights"]
    def manager():
        m=ComponentsManager();m.enable_auto_cpu_offload(device="cuda:0",memory_reserve_margin="12GB");return m
    def chunk_ff(fn):
        def forward(x):
            for span in x.split(1024,dim=-2):span.copy_(fn(span))
            return x
        return forward
    args=dict(width=cfg["width"],height=cfg["height"],num_frames=cfg["frames"],num_inference_steps=50,
              generator=torch.Generator().manual_seed(cfg["seed"]),output=["videos","audio","sampling_rate"])
    with torch.inference_mode():
        if not cfg["first"]:
            flow=ModularPipeline.from_pretrained(weights).blocks.get_workflow("t2va")
            em=manager()
            encoder=flow.sub_blocks.pop("text_encoder").init_pipeline(weights,components_manager=em)
            encoder.load_components(dtype=torch.bfloat16)
            args["state"]=encoder(prompt=cfg["prompt"])
            del encoder,em
            gc.collect();torch.cuda.empty_cache()
            m=manager();pipe=flow.init_pipeline(weights,components_manager=m)
        else:
            m=manager();pipe=ModularPipeline.from_pretrained(weights,workflow="fl2va",components_manager=m)
            args.update(prompt=cfg["prompt"],image=Image.open(cfg["first"]).convert("RGB"))
        pipe.load_components(dtype=torch.bfloat16)
        for block in pipe.transformer.transformer_blocks:block.ff.forward=chunk_ff(block.ff.forward)
        pipe.vae.enable_tiling(tile_sample_min_height=512,tile_sample_min_width=512)
        counter=[0]
        def step(*unused):
            counter[0]+=1
            Path(cfg["step_file"]).write_text(str(counter[0]))
        pipe.transformer.register_forward_hook(step)
        result=pipe(**args)
        encode_video(result["videos"][0],fps=24,output_path=cfg["output"],audio=result["audio"][0],audio_sample_rate=result["sampling_rate"])


def render(runtime,job,ix,piece,previous,update,size,seed):
    if job.get("precision")!="bf16":raise ValueError("BF16 执行端拒绝其他模型任务")
    seconds=float(job["shots"][ix]["seconds"])
    native,n=frame_plan(seconds)
    with tempfile.TemporaryDirectory(dir="/content") as td:
        tmp=Path(td);base=tmp/"original.mp4";first=tmp/"first.png";steps=tmp/"step"
        if previous and Path(previous).suffix==".png":first.write_bytes(Path(previous).read_bytes())
        elif previous:
            subprocess.run(["ffmpeg","-loglevel","error","-sseof","-0.18","-i",str(previous),"-frames:v","1",str(first)],check=True)
        cfg=dict(weights=runtime[1],first=str(first) if previous else None,seed=seed,frames=native,
                 prompt=job["shots"][ix]["prompt"],width=size[0],height=size[1],step_file=str(steps),output=str(base))
        log=Path("/content/h3-bf16-worker-last.log")
        with log.open("w") as out:
            proc=subprocess.Popen([runtime[0],"-u",str(Path(__file__).resolve())],stdin=subprocess.PIPE,stdout=out,stderr=subprocess.STDOUT,text=True,
                env={**os.environ,"HF_HUB_OFFLINE":"1","TRANSFORMERS_OFFLINE":"1","PYTORCH_CUDA_ALLOC_CONF":"expandable_segments:True"})
            try:
                proc.stdin.write(json.dumps(cfg));proc.stdin.close()
                while True:
                    k=steps.read_text().strip() if steps.is_file() else ""
                    note="去噪 "+k+"/49" if k.isdigit() else "加载模型"
                    update("running",4,f"BF16 · 第 {ix+1}/{len(job['shots'])} 镜 · {note}")
                    try:rc=proc.wait(timeout=25);break
                    except subprocess.TimeoutExpired:pass
                if rc:raise RuntimeError("BF16 生成失败，详见执行端日志")
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:proc.kill();proc.wait()
        # Shots shorter than the model's minimum are trimmed; only the small gap at the 15 s limit is retimed.
        ratio=max(1,n/native); film=tmp/"shot.mp4"
        # Separate finite streams avoid a scheduler deadlock between tpad, fps and apad.
        vfile=tmp/"v.mp4";afile=tmp/"a.m4a"
        def ff(*opts):subprocess.run(["ffmpeg","-nostdin","-loglevel","error","-y",*map(str,opts)],timeout=900,check=True)
        ff("-i",base,"-an","-vf",f"setpts=PTS*{ratio},fps=24,tpad=stop_mode=clone:stop_duration=0.2","-frames:v",n,"-c:v","libx264","-preset","fast","-crf","17",vfile)
        ff("-i",base,"-vn","-af",f"atempo={1/ratio},apad=pad_dur=1","-t",seconds,"-c:a","aac","-b:a","192k",afile)
        ff("-i",vfile,"-i",afile,"-map","0:v:0","-map","1:a:0","-c","copy","-t",seconds,"-movflags","+faststart",film)
        dest=piece.with_suffix(".part");shutil.copyfile(film,dest);dest.replace(piece)


if __name__=="__main__":generate(json.load(sys.stdin))
