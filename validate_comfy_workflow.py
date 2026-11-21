"""Exercise the real video worker without loading models or requiring a GPU."""
import ast,copy,hashlib,json,math,shutil,tempfile,uuid
from pathlib import Path
from types import SimpleNamespace as NS
import requests

class StopWaiting(BaseException):pass

def run():
    source=Path(__file__).with_name('colab-worker.py').read_text()
    functions=[n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name in ('durable','_render_piece','comfy_state')]
    with tempfile.TemporaryDirectory() as td:
        root=Path(td);output=root/'output';output.mkdir();(output/'shot.mp4').write_bytes(b'rendered video')
        graph={key:{'inputs':{}} for key in ('140:131','140:129','92')}
        queued={};history={};posts=[];ticks=[0];updates=[];lose_reply=[True];failure=[False];missing=[False]
        def reply(data,status=200):
            r=requests.Response();r.status_code=status;r._content=json.dumps(data).encode();return r
        def get(url,**kw):
            if '/history/' in url:
                cid=url.rsplit('/',1)[1]
                return reply({cid:history[cid]} if cid in history else {})
            assert url.endswith('/queue'),url
            return reply({'queue_running':[(0,cid,g) for cid,g in queued.items()],'queue_pending':[]})
        def post(url,**kw):
            assert url.endswith('/prompt');payload=kw['json'];cid=payload.get('prompt_id',str(uuid.uuid4()))
            posts.append(cid);queued[cid]=payload['prompt']
            if lose_reply[0]:lose_reply[0]=False;raise requests.Timeout('accepted; response lost')
            return reply({'prompt_id':cid})
        def sleep(_):
            ticks[0]+=1
            if ticks[0]>15:raise StopWaiting('worker waited forever after its prompt disappeared')
            if missing[0]:queued.clear();return
            for cid in list(queued):
                history[cid]={'status':{'status_str':'error' if failure[0] else 'success'},'outputs':{'92':{'videos':[{'filename':'shot.mp4','subfolder':''}]}}}
                queued.pop(cid)
        env=dict(json=json,uuid=uuid,hashlib=hashlib,math=math,copy=copy,shutil=shutil,Path=Path,PRECISION='quantized',graph=graph,repo=root,base='http://comfy.test',dimensions=lambda j:(768,448),requests=NS(get=get,post=post,Timeout=requests.Timeout,ConnectionError=requests.ConnectionError),time=NS(monotonic=lambda:100+ticks[0]*4,sleep=sleep))
        exec(compile(ast.Module(body=functions,type_ignores=[]),'<comfy worker>','exec'),env)
        job={'id':'a'*32,'prompt':'scene','shots':[{'seconds':3,'prompt':'scene'}]}
        path=root/'job';path.mkdir();piece=path/'00.mp4'
        def render():return env['_render_piece'](job,0,path,piece,None,lambda *x:updates.append(x))
        try:render()
        except requests.Timeout:pass
        else:raise AssertionError('Expected simulated lost response')
        render()
        assert len(posts)==1,'Accepted prompt was submitted again after a lost response'
        assert piece.read_bytes()==b'rendered video'
        render();assert len(posts)==1,'Completed prompt should reuse history'
        print('PASS accepted submission survives lost response and completed history is reused')

        # Legacy track files must still attach to the existing prompt.
        track=path/'0-queue.json';track.write_text(json.dumps({'id':posts[0]}));render();assert len(posts)==1
        print('PASS legacy queue markers remain compatible')

        history.clear();queued.clear();ticks[0]=0;missing[0]=True
        try:render()
        except RuntimeError:pass
        else:raise AssertionError('Missing prompt must stop instead of looping or resubmitting')
        assert len(posts)==1 and ticks[0]<=15
        print('PASS a vanished prompt stops without duplicate GPU work')

        track.unlink();missing[0]=False;ticks[0]=0;failure[0]=True
        try:render()
        except RuntimeError:pass
        else:raise AssertionError('ComfyUI execution failure must reach the worker')
        assert len(posts)==2
        print('PASS execution failures remain terminal')

        # Failed reads cannot be mistaken for a missing prompt, or trigger new work.
        for status in (429,503):
            env['requests'].get=lambda *a,**kw:reply({'error':'temporary'},status)
            try:render()
            except requests.HTTPError as exc:assert exc.response.status_code==status
            else:raise AssertionError('Read error was ignored')
            assert len(posts)==2
        print('PASS temporary API errors retain their status and never resubmit')

        # Completion may race the queue read; check history again before declaring loss.
        cid=posts[0];reads=[reply({}),reply({'queue_running':[],'queue_pending':[]}),reply({cid:{'outputs':{'92':{}}}})]
        env['requests'].get=lambda *a,**kw:reads.pop(0)
        state,found=env['comfy_state'](cid)
        assert found and state['outputs']=={'92':{}} and not reads
        print('PASS completion racing a queue read is recovered from history')

        # hd_fast must combine full 768p HD (1344x768) with the 8-step Turbo LoRA.
        dim_fn=[n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='dimensions']
        import re as _re
        dim_env={'re':_re}
        exec(compile(ast.Module(body=dim_fn,type_ignores=[]),'<dims>','exec'),dim_env)
        assert dim_env['dimensions']({'quality':'hd_fast','prompt':'scene','shots':[{'prompt':'scene'}]})==(1344,768)
        assert dim_env['dimensions']({'quality':'fast','prompt':'scene','shots':[{'prompt':'scene'}]})==(768,448)
        lora=root/'models'/'loras'/'minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors'
        lora.parent.mkdir(parents=True,exist_ok=True)
        with lora.open('wb') as f:f.truncate(1956193000)
        env['models']=root/'models';env['dimensions']=dim_env['dimensions']
        env['graph']={key:{'inputs':{}} for key in ('140:131','140:129','140:124','140:126','140:127','92')}
        env['requests'].get=get
        submitted=[]
        def post_capture(url,**kw):
            submitted.append(kw['json']['prompt'])
            return post(url,**kw)
        env['requests'].post=post_capture
        hd_fast_job={'id':'b'*32,'quality':'hd_fast','prompt':'scene','shots':[{'seconds':3,'prompt':'scene'}]}
        p2=root/'job2';p2.mkdir();piece2=p2/'00.mp4';failure[0]=False;missing[0]=False;ticks[0]=0
        env['_render_piece'](hd_fast_job,0,p2,piece2,None,lambda *x:None)
        assert submitted[-1]['140:131']['inputs']['width']==1344 and submitted[-1]['140:131']['inputs']['height']==768
        assert submitted[-1]['140:124']['inputs']['steps']==8 and 'turbo' in submitted[-1]
        print('PASS hd_fast renders full 1344x768 768p with 8-step Turbo LoRA')

if __name__=='__main__':run()
