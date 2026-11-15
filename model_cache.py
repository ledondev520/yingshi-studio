"""Authenticated, streaming access to immutable model files in private Drive storage."""
import json
import re
import subprocess
import sys
import threading
from pathlib import Path

MANIFEST = json.loads(Path(__file__).with_name('model-cache-manifest.json').read_text())
STATE = Path('/var/lib/h3-autowake/model-cache-drive.json')
PYTHON = '/opt/h3-studio/autowake-venv/bin/python'
slots = threading.BoundedSemaphore(4)


def entry(sha):
    expected = next((v for v in MANIFEST.values() if v['sha256'] == sha), None)
    if not expected or not STATE.is_file():
        return None
    cached = json.loads(STATE.read_text()).get('files', {}).get(sha, {})
    if (cached.get('verified') is not True or cached.get('size') != expected['size']
            or not re.fullmatch(r'[A-Za-z0-9_-]{10,160}', cached.get('id', ''))):
        return None
    return cached


def serve(h, sha, fail):
    h.worker_auth({'worker_id': h.headers.get('X-H3-Worker-ID'), 'gpu': h.headers.get('X-H3-GPU')})
    item = entry(sha)
    if item is None:
        fail(404, '此模型持久缓存尚未就绪')
    start = 0
    raw = h.headers.get('Range')
    if raw:
        match = re.fullmatch(r'bytes=(\d+)-', raw)
        if not match or int(match[1]) >= item['size']:
            fail(416, '无效的模型读取范围')
        start = int(match[1])
    if not slots.acquire(False):
        fail(429, '模型缓存读取繁忙，请稍后重试')
    proc = None
    try:
        # The existing auth environment keeps Google credentials on the server.
        proc = subprocess.Popen([PYTHON, str(Path(__file__).resolve()), sha, str(start)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        first = proc.stdout.readline(1024)
        if not first or json.loads(first).get('size') != item['size'] - start:
            fail(503, '云盘缓存暂不可用')
        h.send_response(206 if raw else 200)
        for key, value in {'Content-Type': 'application/octet-stream', 'Content-Length': str(item['size'] - start),
                           'Accept-Ranges': 'bytes', 'Cache-Control': 'private, no-store',
                           'X-Content-SHA256': sha}.items():
            h.send_header(key, value)
        if raw:
            h.send_header('Content-Range', f'bytes {start}-{item["size"]-1}/{item["size"]}')
        h.end_headers()
        h.connection.settimeout(90)
        left = item['size'] - start
        while left:
            chunk = proc.stdout.read(min(1024 * 1024, left))
            if not chunk:
                h.close_connection = True
                return
            h.wfile.write(chunk)
            left -= len(chunk)
    finally:
        if proc:
            proc.stdout.close()
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
        slots.release()


def stream(sha, start):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import AuthorizedSession
    sys.path.insert(0, str(Path(__file__).parent / 'autowake'))
    from colab_rest import PROJECT
    item = entry(sha)
    if item is None or not 0 <= start < item['size']:
        raise ValueError('Invalid cache request')
    auth = AuthorizedSession(Credentials.from_authorized_user_file('/var/lib/h3-autowake/.config/colab-cli/token.json'))
    auth.headers['X-Goog-User-Project'] = PROJECT
    with auth.get('https://www.googleapis.com/drive/v3/files/' + item['id'], params={'alt': 'media'},
                  headers={'Range': f'bytes={start}-'} if start else {}, stream=True, timeout=(10, 45)) as response:
        response.raise_for_status()
        if start and response.status_code != 206:
            raise ValueError('Drive did not honor range')
        if int(response.headers.get('Content-Length', -1)) != item['size'] - start:
            raise ValueError('Cached object length changed')
        print(json.dumps({'size': item['size'] - start}), flush=True)
        for chunk in response.iter_content(1024 * 1024):
            sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()


if __name__ == '__main__':
    stream(sys.argv[1], int(sys.argv[2]))
