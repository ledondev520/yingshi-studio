"""Reconnect to an executing kernel without sending or repeating any code."""
import threading,time,uuid
from urllib.parse import quote,urlencode

class KernelLink:
    def __init__(self,state,kernel_id):
        import websocket
        if not state.url.startswith('https://'):raise ValueError('HTTPS runtime URL required')
        self.endpoint=state.endpoint;self.stopped=threading.Event();self.error=None
        query=urlencode({'session_id':uuid.uuid4().hex,'colab-runtime-proxy-token':state.token})
        url=state.url.replace('https://','wss://',1).rstrip('/')+'/api/kernels/'+quote(kernel_id,safe='')+'/channels?'+query
        self.socket=websocket.create_connection(url,header={'X-Colab-Runtime-Proxy-Token':state.token,'X-Colab-Client-Agent':'colab-cli'},timeout=15)
        self.thread=threading.Thread(target=self._receive,daemon=True);self.thread.start()
    def _receive(self):
        import websocket
        received=time.monotonic()
        try:
            while not self.stopped.is_set():
                try:
                    opcode,_=self.socket.recv_data(control_frame=True)
                    if opcode==websocket.ABNF.OPCODE_CLOSE:break
                    received=time.monotonic()
                except websocket.WebSocketTimeoutException:
                    if time.monotonic()-received>60:raise TimeoutError('Kernel transport stopped responding')
                    self.socket.ping()
        except Exception as exc:self.error=type(exc).__name__
        finally:self.socket.close(timeout=1)
    def is_alive(self):return self.socket.connected and self.thread.is_alive()
    def close(self):
        self.stopped.set();self.socket.close(timeout=1);self.thread.join(timeout=2)
