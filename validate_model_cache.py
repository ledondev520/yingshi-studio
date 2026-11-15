"""Exercise private cache auth, ranges, SHA validation, and safe partial-file recovery."""
import hashlib
import io
import json
import runpy
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import model_cache
import server


def run():
    from types import SimpleNamespace
    # Drive may stream a valid object without Content-Length.
    assert model_cache.response_length(SimpleNamespace(status_code=200, headers={}), 100, 0) == 100
    assert model_cache.response_length(SimpleNamespace(status_code=206, headers={'Content-Range':'bytes 31-99/100'}), 100, 31) == 69
    for status, headers, start in [(200, {}, 31), (200, {'Content-Length':'99'}, 0), (206, {'Content-Range':'bytes 0-99/100'}, 31)]:
        try:
            model_cache.response_length(SimpleNamespace(status_code=status, headers=headers), 100, start)
            raise AssertionError('Invalid upstream length/range accepted')
        except ValueError:
            pass
    blob = b'verified-model-weights' * 100
    sha = hashlib.sha256(blob).hexdigest()
    meta = {'model.safetensors': {'size': len(blob), 'sha256': sha}}
    client = runpy.run_path(str(Path(__file__).parent / 'autowake/cache_client.py'),
                           init_globals={'MODEL_CACHE_MANIFEST': meta})['restore_cached_model']
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); server.DATA = server.KEYS = root; server.setup()
        (root / 'worker-key').write_text('cache-test-only')
        state = root / 'model-cache-drive.json'
        state.write_text(json.dumps({'files': {sha: {'id': 'private-drive-file-id', 'size': len(blob), 'verified': True}}}))
        http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{http.server_port}'
        calls = []
        corrupt = [False]
        class Download:
            def __init__(self, args, **kw):
                start = int(args[-1]); calls.append(start)
                data = (b'x' * len(blob) if corrupt[0] else blob)[start:]
                self.stdout = io.BytesIO(json.dumps({'size': len(data)}).encode() + b'\n' + data)
            def poll(self): return 0
            def wait(self, **kw): return 0
        try:
            with patch.object(model_cache, 'MANIFEST', meta), patch.object(model_cache, 'STATE', state), patch.object(model_cache.subprocess, 'Popen', Download):
                try:
                    urllib.request.urlopen(url + '/api/worker/model-cache/' + sha)
                    raise AssertionError('Unauthenticated model download was allowed')
                except urllib.error.HTTPError as exc:
                    assert exc.code == 403
                assert not calls
                dest = root / 'download/model.safetensors'; dest.parent.mkdir()
                part = dest.with_name(dest.name + '.cache.part'); part.write_bytes(blob[:31])
                assert client(dest, 'model.safetensors', url, 'cache-test-only', 'worker', 'A100')
                assert calls == [31] and dest.read_bytes() == blob and not part.exists()
                # A corrupt response cannot replace the already valid destination.
                corrupt[0] = True
                try:
                    client(dest, 'model.safetensors', url, 'cache-test-only', 'worker', 'A100')
                    raise AssertionError('Corrupt model accepted')
                except ValueError:
                    pass
                assert dest.read_bytes() == blob and not part.exists()
                state.write_text(json.dumps({'files': {sha: {'id': 'private-drive-file-id', 'size': len(blob), 'verified': False}}}))
                count = len(calls)
                assert client(dest, 'model.safetensors', url, 'cache-test-only', 'worker', 'A100') is False
                assert len(calls) == count
            print('PASS private auth, partial range restore, pinned SHA, corruption preserves original, unpublished cache fallback')
        finally:
            http.shutdown(); http.server_close()


if __name__ == '__main__':
    run()


def generated_worker_reuses_files():
    import ast
    from concurrent.futures import ThreadPoolExecutor
    tree = ast.parse(Path(__file__).with_name('autowake').joinpath('cold-worker.py').read_text())
    prepare = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'prepare_model')
    calls = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        def download(**kw):
            assert kw['token'] is False
            calls.append(kw['filename'])
            path = root / kw['filename']; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'weights')
        env = dict(models=root, BASE='https://example.invalid', token='test', did='test', gpu_present='A100',
                   restore_cached_model=lambda *a: False, hf_hub_download=download)
        exec(compile(ast.Module(body=[prepare], type_ignores=[]), '<generated-worker>', 'exec'), env)
        for _ in range(2):
            with ThreadPoolExecutor(2) as pool:
                list(pool.map(env['prepare_model'], [('a.bin', 7), ('b.bin', 7)]))
        assert sorted(calls) == ['a.bin', 'b.bin']
    print('PASS generated worker prepares files concurrently and reuses intact local models')


if __name__ == '__main__':
    generated_worker_reuses_files()
