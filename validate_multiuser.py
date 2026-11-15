"""Exercise 32 independent video consumers through HTTP; no model or paid GPU calls."""
import json
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

import server


def run():
    with tempfile.TemporaryDirectory() as tmp:
        server.DATA = server.KEYS = Path(tmp)
        server.setup()
        (server.KEYS / 'worker-key').write_text('capacity-test-only')
        http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        server.ORIGIN = f'http://127.0.0.1:{http.server_port}'
        threading.Thread(target=http.serve_forever, daemon=True).start()

        def request(path, data=None, cookie='', worker=False, status=200):
            headers = {'Origin': server.ORIGIN, 'Content-Type': 'application/json',
                       'Cookie': cookie, 'X-Real-IP': '192.0.2.10'}
            if worker:
                headers['Authorization'] = 'Bearer capacity-test-only'
            body = json.dumps(data).encode() if data is not None else None
            req = urllib.request.Request(server.ORIGIN + '/api/' + path, data=body, headers=headers)
            try:
                response = urllib.request.urlopen(req, timeout=15)
            except urllib.error.HTTPError as exc:
                response = exc
            result = json.loads(response.read())
            assert response.code == status, (path, response.code, status, result)
            return result, response.headers.get('Set-Cookie', '').split(';')[0]

        def create(cookie, status=200):
            return request('create', {'request_id': uuid.uuid4().hex, 'prompt': '海边的一只小船',
                                      'seconds': 30, 'quality': 'standard'}, cookie, status=status)[0]

        def claim(index, gpu='NVIDIA A100-SXM4-80GB', status=200):
            return request('worker/claim', {'worker_id': f'capacity-{index}',
                           'runtime_id': f'{index:024x}', 'gpu': gpu, 'shot_v1': True,
                           'relay_v1': True, 'finish_v2': True, 'append_v1': True},
                           worker=True, status=status)[0]

        try:
            cookies = [request('state')[1] for _ in range(10)]
            users = {cookie: [] for cookie in cookies}
            # Ten users share one public IP; each can submit more than two jobs.
            for cookie in cookies:
                for _ in range(3):
                    users[cookie].append(create(cookie)['job']['id'])
            for cookie in cookies[:2]:
                users[cookie].append(create(cookie)['job']['id'])
            state = request('state', cookie=cookies[0])[0]
            assert {j['id'] for j in state['jobs']} == set(users[cookies[0]])
            request('creation/' + users[cookies[1]][0], cookie=cookies[0], status=404)

            shots = json.dumps([{'seconds': 10, 'prompt': '海边', 'cut': i > 0} for i in range(3)])
            with server.db() as c:
                c.execute("UPDATE jobs SET state='queued',shots=?,route='story',audio_mode='original'", (shots,))
                assert len(server.fleet.need(c, server.now())) >= 32
            assert server.fleet.MAX_CARDS >= 32
            # Existing jobs offer helpers, but waiting users must receive primary workers first.
            first = claim(0)
            assert first['job'] and first['assist'] is None
            with ThreadPoolExecutor(max_workers=10) as pool:
                results = [first, *pool.map(claim, range(1, 32))]
            ids = [r['job']['id'] for r in results if r['job']]
            assert len(ids) == len(set(ids)) == 32, 'An early video consumed another waiting user’s card'
            assert all(r['assist'] is None for r in results)
            assert claim(32)['assist'], 'Spare cards should still help independent shots'
            claim(33, gpu='NVIDIA L4', status=422)
            claim(34, gpu='Tesla T4', status=422)
            for cookie in cookies:
                assert request('state', cookie=cookie)[0]['jobs']

            # Exhausted leases keep saved work queued even after more than three runtime losses.
            with server.db() as c:
                c.execute("UPDATE jobs SET deadline=?,tries=8 WHERE id=?", (server.now() - 1, ids[0]))
                server.cleanup(c)
                row = c.execute('SELECT state,lease,wid,tries FROM jobs WHERE id=?', (ids[0],)).fetchone()
                assert tuple(row) == ('queued', None, None, 8)

            cookie = cookies[0]
            for _ in range(10 - len(users[cookie])):
                create(cookie)
            create(cookie, status=429)
            with server.db() as c:
                active = c.execute("SELECT count(*) FROM jobs WHERE state IN ('planning','queued','running')").fetchone()[0]
                for i in range(active, 320):
                    c.execute("INSERT INTO jobs(id,state,shots,precision) VALUES(?,'queued','[]','quantized')", (f'fill-{i}',))
            create(cookies[2], status=429)
            print('PASS 10 users on one IP, 32 independent claims, visitor isolation, helper fallback, '
                  'L4/T4 rejection, repeated-loss requeue, per-user and global queue bounds')
        finally:
            http.shutdown()
            http.server_close()


if __name__ == '__main__':
    run()
