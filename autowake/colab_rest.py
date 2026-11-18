"""Official Colab MCP lifecycle with scoped REST recovery; no browser cookies."""
import time,uuid,requests,base64,json
from pathlib import Path
from urllib.parse import quote
from datetime import datetime
from types import SimpleNamespace as NS
HOST='https://colaboratory.googleapis.com/'
def _resolve_project(default='YOUR_GOOGLE_CLOUD_PROJECT'):
    import json,os
    for v in (os.environ.get('H3_COLAB_PROJECT'),os.environ.get('H3_GCP_PROJECT'),os.environ.get('GOOGLE_CLOUD_PROJECT')):
        if v and v.strip() and v.strip()!=default:return v.strip()
    for p in (Path('/etc/h3-studio/colab-project'),Path.home()/'.config/colab-cli/colab-project',Path('/etc/h3-studio/gcp-project'),Path.home()/'.config/colab-cli/gcp-project'):
        try:
            if p.is_file() and (t:=p.read_text().strip()) and t!=default:return t
        except OSError:pass
    try:
        tp=Path.home()/'.config/colab-cli/token.json'
        if tp.is_file() and (qp:=json.loads(tp.read_text()).get('quota_project_id')) and qp.strip():return qp.strip()
    except Exception:pass
    return default
PROJECT=_resolve_project()
class Capacity(Exception):pass
def wrap(x):
    spec=x.get('runtimeSpec') or {};info=x.get('connectionInfo') or {};expiry=info.get('expireTime') or '1970-01-01T00:00:00Z'
    return NS(endpoint=x['name'],accelerator=NS(value=spec.get('accelerator','')),runtime_proxy_info=NS(url=info.get('url',''),token=info.get('token',''),expires_at=lambda:datetime.fromisoformat(expiry.replace('Z','+00:00'))))
class PublicColab:
    def __init__(self,auth):self.s=auth
    def mcp(self,tool,arguments):
        call={'jsonrpc':'2.0','id':uuid.uuid4().hex,'method':'tools/call','params':{'name':tool,'arguments':arguments}}
        r=self.s.request('POST',HOST+'mcp',headers={'Accept':'application/json, text/event-stream'},json=call,timeout=(10,300))
        r.raise_for_status()
        if r.headers.get('Content-Type','').split(';')[0].strip()=='text/event-stream':
            # SSE events may span multiple data lines; only the matching terminal reply counts.
            reply=None;data=[]
            for line in r.text.replace('\r\n','\n').split('\n')+['']:
                if line.startswith('data:'):data.append(line[5:].lstrip(' '))
                elif not line and data:
                    event=json.loads('\n'.join(data));data=[]
                    if isinstance(event,dict) and event.get('id')==call['id']:reply=event
        else:reply=r.json()
        if not isinstance(reply,dict) or reply.get('id')!=call['id'] or 'error' in reply:raise ValueError('MCP protocol error')
        result=reply['result']
        if not isinstance(result,dict) or result.get('isError'):raise ValueError('MCP tool error')
        data=result['structuredContent']
        if not isinstance(data,dict):raise ValueError('MCP result must be an object')
        if tool in ('get_runtime','create_runtime'):
            expected=arguments.get('name') or 'runtimes/'+arguments['runtimeId']
            if data.get('name')!=expected:raise ValueError('MCP returned another runtime')
        return data
    def req(self,method,path,**kw):
        tool=None;arguments={}
        if method=='GET' and path=='v1beta/runtimes':tool='list_runtimes'
        elif method=='GET' and path.startswith('v1beta/runtimes/'):tool='get_runtime';arguments={'name':path.removeprefix('v1beta/')}
        elif method=='DELETE' and path.startswith('v1beta/runtimes/'):
            tool='delete_runtime';arguments={'name':path.removeprefix('v1beta/'),'allowMissing':True}
        elif method=='POST' and path=='v1beta/runtimes':
            tool='create_runtime';arguments={'runtime':kw['json'],**kw['params']}
        if tool:
            try:
                data=self.mcp(tool,arguments)
                return {'done':True,'response':data} if tool=='create_runtime' else data
            except (requests.RequestException,ValueError,KeyError,TypeError):
                # The same runtimeId/requestId safely recovers a timed-out create; REST retains ErrorInfo reasons.
                print('Colab MCP',tool,'using REST recovery',flush=True)
        r=self.s.request(method,HOST+path,timeout=35,**kw);r.raise_for_status();return r.json()
    def list_assignments(self):
        return [wrap(x) for x in self.req('GET','v1beta/runtimes').get('runtimes',[])]
    def get(self,name):return wrap(self.req('GET','v1beta/'+name))
    def op(self,op):
        for _ in range(120):
            if op.get('done'):return op
            time.sleep(2);op=self.req('GET','v1/'+op['name'])
        raise TimeoutError('Google allocation pending')
    def assign(self,stable,variant,model,highmem=False):
        name='h3-'+stable.hex[:24]
        try:return self.get('runtimes/'+name)
        except requests.HTTPError as e:
            if e.response.status_code!=404:raise
        shapes=('SHAPE_HIGHMEM',) if highmem and model.value=='A100' else (('SHAPE_HIGHMEM','SHAPE_STANDARD') if model.value=='A100' else ('SHAPE_STANDARD',))
        for shape in shapes:
            spec={'variant':'VARIANT_GPU','accelerator':model.value,'shape':shape}
            try:
                op=self.op(self.req('POST','v1beta/runtimes',params={'runtimeId':name,'requestId':str(uuid.uuid4())},json={'runtimeSpec':spec}))
            except requests.HTTPError as e:
                if e.response.status_code not in (400,412,429,503):raise
                reason=''
                try:reason=next((d.get('reason') for d in (e.response.json().get('error') or {}).get('details',[]) if d.get('reason')),'')
                except Exception:pass
                if shape!=shapes[-1] and reason!='TOO_MANY_ACTIVE_RUNTIMES':continue
                raise Capacity(reason or ('HTTP '+str(e.response.status_code))) from e
            if op.get('error'):
                err=op['error']
                if err['code']==6:return self.get('runtimes/'+name)
                if err['code'] in (8,9):
                    reason=next((x.get('reason') for x in err.get('details',[]) if x.get('reason')),str(err['code']))
                    print('Colab',model.value,shape,'allocation refused:',reason,str(err.get('message',''))[:180],flush=True)
                    if shape!=shapes[-1] and reason!='TOO_MANY_ACTIVE_RUNTIMES':continue
                    raise Capacity(reason)
                raise RuntimeError('Colab allocation: '+str(err['code']))
            return self.get('runtimes/'+name)
    def unassign(self,name):
        # DeleteRuntime is synchronous and returns {}, unlike CreateRuntime.
        return self.req('DELETE','v1beta/'+name,params={'allowMissing':'true'})

class RuntimeFiles:
    def __init__(self,state):
        self.url=state.url.rstrip('/')
        self.headers={'X-Colab-Runtime-Proxy-Token':state.token}
    def upload(self,local,name):
        remote='content/'+name
        body={'type':'file','format':'base64','content':base64.b64encode(Path(local).read_bytes()).decode()}
        r=requests.put(self.url+'/api/contents/'+quote(remote,safe='/'),headers=self.headers,json=body,timeout=(10,45),allow_redirects=False)
        r.raise_for_status()
