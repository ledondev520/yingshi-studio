"""Check the submitted model graph without loading weights or allocating a GPU."""
import ast,copy,hashlib,math,tempfile
from pathlib import Path
from types import SimpleNamespace

tree=ast.parse(Path(__file__).with_name('colab-worker.py').read_text())
render=next(n for n in tree.body if getattr(n,'name','')=='_render_piece')
graph=ast.literal_eval(next(n.value for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='graph' for t in n.targets)))
class Submitted(Exception):pass

def submitted(gpu,quality):
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);models=root/'models';(models/'loras').mkdir(parents=True)
        # A sparse placeholder exercises the existing cached Turbo branch without a download.
        with (models/'loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors').open('wb') as f:f.truncate(1956193000)
        def post(url,**kw):raise Submitted(kw['json']['prompt'])
        env=dict(copy=copy,hashlib=hashlib,math=math,graph=graph,gpu_name=gpu,PRECISION='quantized',repo=root,models=models,base='http://test',requests=SimpleNamespace(post=post),dimensions=lambda job:(1344,768))
        exec(compile(ast.Module(body=[render],type_ignores=[]),'<worker>','exec'),env)
        job=dict(id='same-seed',prompt='海',quality=quality,shots=[dict(seconds=15,prompt='浪')])
        try:env['_render_piece'](job,0,root/'same-job',root/'00.mp4',None,lambda *args:None)
        except Submitted as sent:return sent.args[0]
        raise AssertionError('No graph submitted')

def run():
    baseline=submitted('NVIDIA RTX PRO 6000 Blackwell Server Edition','hd')
    for gpu in ('NVIDIA A100-SXM4-40GB','NVIDIA A100-SXM4-80GB'):
        g=submitted(gpu,'hd');backend=g['attention']
        assert backend=={'class_type':'ModelAttentionBackend','inputs':{'model':['140:127',0],'attention':'comfy kitchen attention'}}
        assert g['140:124']['inputs']['model']==g['140:126']['inputs']['model']==['attention',0]
        # Only the video model's attention changes; seed, text, VAE, geometry and 20 steps stay intact.
        del g['attention']
        for n in ('140:124','140:126'):g[n]['inputs']['model']=['140:127',0]
        assert g==baseline
    for gpu in ('NVIDIA A100-SXM4-80GB','NVIDIA H100 80GB HBM3','NVIDIA RTX PRO 6000 Blackwell Server Edition'):
        for quality in ('fast','standard','hd'):
            g=submitted(gpu,quality)
            if 'A100' not in gpu or quality!='hd':assert 'attention' not in g
            if quality=='fast':assert g['140:124']['inputs']['steps']==8 and g['140:126']['inputs']['model']==['turbo',0]
    print('PASS A100 HD attention routing, unchanged seed/geometry/20 steps, G4/H100 and Turbo isolation')

if __name__=='__main__':run()
