"""Check synchronous release and transport-only attachment without a cloud VM."""
import queue,sys,threading,types
from types import SimpleNamespace
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlsplit
from autowake.colab_rest import PublicColab
from autowake.kernel_link import KernelLink

class ReadTimeout(Exception):pass
class Socket:
    def __init__(self):
        self.connected=True;self.messages=queue.Queue();self.reading=threading.Event();self.pings=0
    def recv_data(self,control_frame=False):
        assert control_frame;self.reading.set();message=self.messages.get(timeout=2)
        if isinstance(message,Exception):raise message
        return message
    def ping(self):self.pings+=1
    def close(self,timeout=1):self.connected=False;self.messages.put((8,b''))

def run():
    client=PublicColab(Mock());client.req=Mock(return_value={});client.op=Mock(side_effect=AssertionError('DELETE must not be polled as an operation'))
    assert client.unassign('runtimes/finished')=={}
    client.req.assert_called_once_with('DELETE','v1beta/runtimes/finished',params={'allowMissing':'true'});client.op.assert_not_called()
    client.req.side_effect=ConnectionError('network')
    try:client.unassign('runtimes/finished')
    except ConnectionError:pass
    else:raise AssertionError('Failed release must remain retryable')

    import requests,uuid
    from autowake.colab_rest import Capacity
    client2=PublicColab(Mock())
    client2.req=Mock(return_value={'runtimes':[{'name':'runtimes/m-s-cpu'},{'name':'runtimes/h3-'+'1'*24,'runtimeSpec':{'accelerator':'A100'}}]})
    listed=client2.list_assignments()
    assert [x.endpoint for x in listed]==['runtimes/m-s-cpu','runtimes/h3-'+'1'*24] and listed[0].accelerator.value==''
    not_found=requests.HTTPError(response=SimpleNamespace(status_code=404))
    shapes_seen=[]
    def fake_req(method,path,**kw):
        if method=='GET':
            if not shapes_seen:raise not_found
            return {'name':path.removeprefix('v1beta/'),'runtimeSpec':{'accelerator':'A100'}}
        shapes_seen.append(kw['json']['runtimeSpec']['shape'])
        if len(shapes_seen)==1:return {'done':True,'error':{'code':8,'details':[{'reason':'NO_RUNTIMES'}]}}
        return {'done':True}
    client2.req=Mock(side_effect=fake_req)
    got=client2.assign(uuid.UUID(int=1),'GPU',SimpleNamespace(value='A100'),highmem=False)
    assert shapes_seen==['SHAPE_HIGHMEM','SHAPE_STANDARD'] and got.accelerator.value=='A100'
    shapes_limit=[]
    def limit_req(method,path,**kw):
        if method=='GET':raise not_found
        shapes_limit.append(kw['json']['runtimeSpec']['shape'])
        return {'done':True,'error':{'code':8,'details':[{'reason':'TOO_MANY_ACTIVE_RUNTIMES'}]}}
    client2.req=Mock(side_effect=limit_req)
    try:client2.assign(uuid.UUID(int=2),'GPU',SimpleNamespace(value='A100'),highmem=False)
    except Capacity as exc:assert str(exc)=='TOO_MANY_ACTIVE_RUNTIMES' and shapes_limit==['SHAPE_HIGHMEM']
    else:raise AssertionError('Expected Capacity on active runtime limit')

    peer=Socket();ws=types.ModuleType('websocket');ws.create_connection=Mock(return_value=peer)
    ws.WebSocketTimeoutException=ReadTimeout;ws.ABNF=SimpleNamespace(OPCODE_CLOSE=8)
    state=SimpleNamespace(endpoint='runtimes/live',url='https://runtime.invalid/',token='test/+')
    with patch.dict(sys.modules,websocket=ws):
        link=KernelLink(state,'existing');assert peer.reading.wait(1) and link.is_alive()
        url=ws.create_connection.call_args.args[0];parsed=urlsplit(url)
        assert parsed.scheme=='wss' and parsed.path=='/api/kernels/existing/channels'
        assert parse_qs(parsed.query)['colab-runtime-proxy-token']==['test/+']
        assert ws.create_connection.call_args.kwargs['timeout']==15
        peer.messages.put((8,b''));link.thread.join(1);assert not link.is_alive()
        # A black-holed connection must become unhealthy, so the controller can retry.
        peer=Socket();peer.messages.put(ReadTimeout());peer.messages.put(ReadTimeout());ws.create_connection.return_value=peer
        with patch('autowake.kernel_link.time.monotonic',side_effect=[0,15,61]):
            link=KernelLink(state,'existing');link.thread.join(1)
        assert not link.is_alive() and link.error=='TimeoutError' and peer.pings==1
        peer=Socket();ws.create_connection.return_value=peer
        link=KernelLink(state,'existing');assert peer.reading.wait(1);link.close();assert not link.is_alive()
    print('PASS synchronous deletion, failed release retry, transport-only reconnect, closed/dead socket detection, bounded cleanup')

if __name__=='__main__':run()
