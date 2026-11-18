"""Exercise the real provisioning boundary without allocating a cloud runtime."""
import json,uuid
from types import SimpleNamespace as NS
from unittest.mock import Mock
import requests
from autowake.colab_rest import PublicColab,Capacity,HOST

def response(body,status=200,sse=False):
    r=requests.Response();r.status_code=status
    r.headers['Content-Type']='text/event-stream' if sse else 'application/json'
    r._content=body.encode() if isinstance(body,str) else json.dumps(body).encode()
    return r

def run():
    runtime={'name':'runtimes/h3-'+uuid.UUID(int=1).hex[:24],'runtimeSpec':{'accelerator':'A100'}}
    calls=[]
    def mcp_reply(method,url,**kw):
        assert method=='POST' and url==HOST+'mcp'
        call=kw['json'];calls.append(call['params'])
        tool=call['params']['name']
        data={'runtimes':[runtime]} if tool=='list_runtimes' else ({} if tool=='delete_runtime' else runtime)
        return response({'jsonrpc':'2.0','id':call['id'],'result':{'structuredContent':data}})
    auth=Mock();auth.request.side_effect=mcp_reply;cl=PublicColab(auth)
    assert cl.list_assignments()[0].accelerator.value=='A100'
    # An existing runtime is reused without another create request.
    assert cl.assign(uuid.UUID(int=1),'GPU',NS(value='A100')).endpoint==runtime['name']
    assert [x['name'] for x in calls]==['list_runtimes','get_runtime']
    assert cl.unassign(runtime['name'])=={} and calls[-1]['arguments']=={'name':runtime['name'],'allowMissing':True}

    def stream_reply(method,url,**kw):
        reply={'jsonrpc':'2.0','id':kw['json']['id'],'result':{'structuredContent':{'runtimes':[runtime]}}}
        encoded=json.dumps(reply,indent=2)
        stream=': keepalive\r\n\r\ndata: {"method":"notifications/progress"}\r\n\r\n'
        stream+='\r\n'.join('data: '+line for line in encoded.splitlines())+'\r\n\r\n'
        return response(stream,sse=True)
    auth.request.side_effect=stream_reply
    assert cl.list_assignments()[0].endpoint==runtime['name']

    # MCP tool errors omit ErrorInfo; REST must preserve true 404 and quota reasons.
    def recovery(method,url,**kw):
        if url==HOST+'mcp':return response({'id':kw['json']['id'],'result':{'isError':True,'content':[{'type':'text','text':'Unavailable'}]}})
        return response({'error':{'details':[{'reason':'TOO_MANY_ACTIVE_RUNTIMES'}]}},412)
    auth.request.side_effect=recovery
    try:cl.req('GET','v1beta/'+runtime['name'])
    except requests.HTTPError as exc:assert exc.response.status_code==412
    else:raise AssertionError('A tool error must never be interpreted as a missing runtime')

    # A timed-out create may already be running: both transports use the same IDs/spec.
    def timed_create(method,url,**kw):
        if url==HOST+'mcp':raise requests.Timeout('pending')
        mcp_call=auth.request.call_args_list[-2].kwargs['json']['params']['arguments']
        assert mcp_call=={'runtime':kw['json'],**kw['params']}
        return response({'done':True,'response':runtime})
    auth.request.reset_mock();auth.request.side_effect=timed_create
    params={'runtimeId':runtime['name'].split('/')[1],'requestId':str(uuid.uuid4())}
    spec={'runtimeSpec':{'variant':'VARIANT_GPU','accelerator':'A100','shape':'SHAPE_HIGHMEM'}}
    assert cl.op(cl.req('POST','v1beta/runtimes',params=params,json=spec))['done']
    assert auth.request.call_count==2

    # Successful creates return a Runtime, not an Operation; never poll it as one.
    auth.request.side_effect=mcp_reply
    cl.get=Mock(side_effect=[requests.HTTPError(response=response({},404)),NS(endpoint=runtime['name'])])
    assert cl.assign(uuid.UUID(int=1),'GPU',NS(value='A100')).endpoint==runtime['name']
    created=calls[-1]
    assert created['name']=='create_runtime' and created['arguments']['runtime']==spec
    assert created['arguments']['runtimeId']==params['runtimeId']
    assert uuid.UUID(created['arguments']['requestId']).version==4

    def quota(method,url,**kw):
        if url==HOST+'mcp':return response({'id':kw['json']['id'],'result':{'isError':True}})
        return response({'done':True,'error':{'code':8,'details':[{'reason':'TOO_MANY_ACTIVE_RUNTIMES'}]}})
    auth.request.side_effect=quota;cl.get.side_effect=requests.HTTPError(response=response({},404))
    try:cl.assign(uuid.UUID(int=1),'GPU',NS(value='A100'))
    except Capacity as exc:assert str(exc)=='TOO_MANY_ACTIVE_RUNTIMES'
    else:raise AssertionError('Quota limit must reach the existing scheduler')

    # A malformed reply, a different runtime, or HTTP rejection recovers via REST.
    for invalid in ('invalid JSON',{'result':{}},runtime):
        def malformed(method,url,**kw):
            if url==HOST+'mcp':return response(invalid)
            return response(runtime)
        auth.request.side_effect=malformed
        assert cl.req('GET','v1beta/'+runtime['name'])==runtime
    def wrong_runtime(method,url,**kw):
        if url==HOST+'mcp':return response({'id':kw['json']['id'],'result':{'structuredContent':{'name':'runtimes/other'}}})
        return response(runtime)
    auth.request.side_effect=wrong_runtime
    assert cl.req('GET','v1beta/'+runtime['name'])==runtime
    auth.request.side_effect=[response({},403),response({'runtimes':[]})]
    assert cl.list_assignments()==[]
    print('PASS MCP reuse/create/delete, JSON/SSE, idempotent REST recovery, quota reasons and protocol failures')

if __name__=='__main__':run()
