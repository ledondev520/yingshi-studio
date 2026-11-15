"""Preparation telemetry never advertises a worker as ready or blocks rendering."""
import threading
from pathlib import Path
REPORTERS=[]

def close_all(failed=False):
    for reporter in REPORTERS:
        reporter.stop.set();reporter.wake.set()
        if reporter.thread.is_alive():reporter.thread.join(timeout=9)
        if failed:
            reporter.stage('failed');reporter.report(reporter.snapshot())
    REPORTERS.clear()

class Preparation:
    def __init__(self, base, identity, mode, gpu, memory=0):
        self.base=base;self.identity=identity;self.mode=mode;self.gpu=gpu;self.memory=memory
        self.lock=threading.Lock();self.wake=threading.Event();self.stop=threading.Event()
        self.phase='environment';self.root=None;self.files={};self.reused=0
        REPORTERS.append(self);self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()

    def stage(self, phase):
        with self.lock:
            if self.phase==phase:return
            self.phase=phase;self.root=None;self.files={};self.reused=0
        self.wake.set()

    @staticmethod
    def completed(root, files):
        sizes=[]
        for name,size in files.items():
            try:
                if (root/name).is_file() and (root/name).stat().st_size==size:sizes.append(size)
            except OSError:pass
        return sum(sizes),len(sizes)

    def weights(self, root, files, phase='download'):
        # Only complete, size-checked files count; temporary/preallocated files are not downloaded bytes.
        files={name:size for name,size in files.items() if name.endswith('.safetensors')}
        with self.lock:
            self.root=Path(root);self.files=files;self.phase=phase
            self.reused=self.completed(self.root,files)[0]
            if files and self.reused==sum(files.values()):self.phase='cache'
        self.wake.set()

    def snapshot(self):
        with self.lock:
            phase,root,files,reused=self.phase,self.root,self.files,self.reused
        done,count=self.completed(root,files) if root else (0,0)
        return dict(stage=phase,bytes_done=done,bytes_total=sum(files.values()),
                    files_done=count,files_total=len(files),reused_bytes=min(done,reused))

    def report(self, snapshot):
        import requests
        try:
            requests.post(self.base+'/api/worker/preparation',
                headers={'Authorization':'Bearer '+self.identity['worker_key']},
                json=dict(worker_id=self.identity['worker_id'],gpu=self.gpu,precision=self.mode,
                          memory_mib=self.memory,**snapshot),timeout=(3,5),allow_redirects=False).raise_for_status()
        except Exception:pass

    def loop(self):
        last=None
        while not self.stop.is_set():
            self.wake.clear();snapshot=self.snapshot()
            if snapshot['stage']!='ready' or snapshot!=last:self.report(snapshot)
            last=snapshot;self.wake.wait(15)
