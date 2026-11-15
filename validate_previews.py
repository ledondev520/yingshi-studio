"""Real transcode and authenticated HTTP checks; no GPU or model calls."""
import hashlib
import json
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import server


def run():
    with tempfile.TemporaryDirectory() as tmp:
        server.DATA = server.KEYS = Path(tmp)
        server.setup()
        (server.KEYS / 'worker-key').write_text('preview-test-only')
        source = Path(tmp) / 'source.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=size=640x360:rate=24:duration=2', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=2', '-c:v', 'libx264', '-crf', '0',
                        '-threads', '2', '-c:a', 'aac', '-movflags', '+faststart', str(source)], check=True)
        original = source.read_bytes()
        sha = hashlib.sha256(original).hexdigest()
        visitor = 'a' * 64
        with server.db() as c:
            for jid in ('11111111111111111111111111111111', '22222222222222222222222222222222', '33333333333333333333333333333333'):
                c.execute("INSERT INTO jobs(id,state,shots,seconds,visitor,result,created) "
                          "VALUES(?,'done','[]',2,?,'https://drive.google.com/file/d/test123456789/view',1)",
                          (jid, server.digest(visitor)))
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{http.server_port}/api/'

        def request(route, status=200, method='GET', data=None, headers=None):
            req = urllib.request.Request(base + route, method=method, data=data, headers=headers or {})
            try:
                response = urllib.request.urlopen(req, timeout=10)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                body = response.read()
                assert response.status == status, (route.split('?')[0], response.status, status, body[:100])
                return body, response.headers

        token = server.watch_token('11111111111111111111111111111111')
        auth = {'X-H3-Watch-Token': token}
        try:
            request('worker/media/11111111111111111111111111111111?drive_id=test123456789', method='PUT', data=original,
                    headers={'Authorization': 'Bearer preview-test-only', 'X-H3-Worker-ID': 'test-worker',
                             'X-H3-GPU': 'A100', 'Content-Type': 'video/mp4', 'X-Content-SHA256': sha})
            # Originals are immediately available, while preview requests never silently fetch the big file.
            assert request('media/11111111111111111111111111111111', headers=auth)[0] == original
            request('preview/11111111111111111111111111111111', status=409, headers=auth)
            assert json.loads(request('creation/11111111111111111111111111111111', headers=auth)[0])['job']['preview_status'] == 'pending'

            # Existing completed uploads are discovered without another PUT or model run.
            with server.db() as c:
                for jid in ('22222222222222222222222222222222', '33333333333333333333333333333333'):
                    server.media.path(server.DATA, jid).write_bytes(original)
                    c.execute('UPDATE jobs SET media_sha=? WHERE id=?', (sha, jid))
            assert server.media.run_preview_once(server.DATA, server.db)
            preview, head = request('preview/11111111111111111111111111111111', headers=auth)
            assert len(preview) < len(original), (len(preview), len(original))
            assert preview.index(b'moov') < preview.index(b'mdat')
            assert head['ETag'] != request('media/11111111111111111111111111111111', headers=auth)[1]['ETag']
            assert 'private' in head['Cache-Control'] and 'no-cache' in head['Cache-Control']
            request('preview/11111111111111111111111111111111', status=304, headers={**auth, 'If-None-Match': head['ETag']})
            part, headers = request('preview/11111111111111111111111111111111', status=206, headers={**auth, 'Range': 'bytes=10-99'})
            assert part == preview[10:100] and headers['Content-Range'] == f'bytes 10-99/{len(preview)}'
            assert request('preview/11111111111111111111111111111111', status=206, headers={**auth, 'Range': 'bytes=-20'})[0] == preview[-20:]
            request('preview/11111111111111111111111111111111', status=416, headers={**auth, 'Range': f'bytes={len(preview)}-'})
            assert request('preview/11111111111111111111111111111111', headers=auth, method='HEAD')[1]['Content-Length'] == str(len(preview))
            assert request('preview/11111111111111111111111111111111', headers={**auth, 'If-Range': '"wrong"', 'Range': 'bytes=0-9'})[0] == preview
            for route in ('media/11111111111111111111111111111111?download=1', 'preview/11111111111111111111111111111111'):
                request(route, status=401)
                request(route, status=404, headers={'Cookie': 'h3_vid=' + 'b' * 64})
                request(route, status=401, headers={'X-H3-Watch-Token': server.watch_token('22222222222222222222222222222222')})
            assert request('preview/11111111111111111111111111111111', headers={'Cookie': 'h3_vid=' + visitor})[0] == preview
            downloaded, headers = request('media/11111111111111111111111111111111?download=1', headers=auth)
            assert downloaded == original and 'attachment' in headers['Content-Disposition']
            assert server.media.path(server.DATA, '11111111111111111111111111111111').read_bytes() == original
            assert json.loads(request('creation/11111111111111111111111111111111', headers=auth)[0])['job']['preview_status'] == 'ready'
            assert server.media.run_preview_once(server.DATA, server.db)
            request('preview/22222222222222222222222222222222', headers={'X-H3-Watch-Token': server.watch_token('22222222222222222222222222222222')})

            # Failed transcoding keeps the source, leaves no partial preview, and retries after backoff.
            with patch('media.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'ffmpeg')):
                assert server.media.run_preview_once(server.DATA, server.db)
            broken_auth = {'X-H3-Watch-Token': server.watch_token('33333333333333333333333333333333')}
            assert request('media/33333333333333333333333333333333', headers=broken_auth)[0] == original
            assert json.loads(request('creation/33333333333333333333333333333333', headers=broken_auth)[0])['job']['preview_status'] == 'failed'
            assert not server.media.run_preview_once(server.DATA, server.db)
            assert not list((server.DATA / 'previews').glob('*.part'))
            with patch('media.time.time', return_value=server.now() + 301):
                assert server.media.run_preview_once(server.DATA, server.db)
            request('preview/33333333333333333333333333333333', headers=broken_auth)

            saved = Path(tmp) / 'preview-check.mp4'
            saved.write_bytes(preview)
            probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams',
                                                       '-show_format', '-of', 'json', str(saved)]))
            assert {s['codec_name'] for s in probe['streams']} == {'h264', 'aac'}
            assert abs(float(probe['format']['duration']) - 2) < .1
            subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(saved), '-f', 'null', '-'], check=True)
            print(f'PASS upload + backfill, smaller H264/AAC preview ({len(original)} -> {len(preview)} bytes), '
                  'faststart, original preservation/download, auth isolation, Range/HEAD/ETag, failure/retry')
        finally:
            http.shutdown()
            http.server_close()


if __name__ == '__main__':
    run()
