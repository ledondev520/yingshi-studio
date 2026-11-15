"""Verify externalized GCP config, graceful SIGTERM drain, and atomic auto-deploy + rollback."""
import json, os, signal, socket, sqlite3, subprocess, sys, tempfile, threading, time, types, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def test_project_resolution():
    import director
    with tempfile.TemporaryDirectory() as td:
        keys = Path(td)
        assert director._resolve_project(keys) == director.PROJECT
        (keys / 'gcp-project').write_text('live-vertex-project-123\n')
        assert director._resolve_project(keys) == 'live-vertex-project-123'
        old = os.environ.get('H3_GCP_PROJECT')
        try:
            os.environ['H3_GCP_PROJECT'] = 'env-override-project'
            assert director._resolve_project(keys) == 'env-override-project'
        finally:
            if old is None: os.environ.pop('H3_GCP_PROJECT', None)
            else: os.environ['H3_GCP_PROJECT'] = old

    sys.modules.setdefault('requests', types.ModuleType('requests'))
    sys.path.insert(0, str(ROOT / 'autowake'))
    import colab_rest
    old_c = os.environ.get('H3_COLAB_PROJECT')
    try:
        os.environ['H3_COLAB_PROJECT'] = 'live-colab-quota-456'
        assert colab_rest._resolve_project() == 'live-colab-quota-456'
    finally:
        if old_c is None: os.environ.pop('H3_COLAB_PROJECT', None)
        else: os.environ['H3_COLAB_PROJECT'] = old_c
    print('PASS externalized Vertex & Colab GCP project ID resolution')


def test_graceful_sigterm_drain():
    with tempfile.TemporaryDirectory() as td:
        port = free_port()
        env = {**os.environ, 'STUDIO_DATA': str(Path(td) / 'data'), 'STUDIO_KEYS': str(Path(td) / 'keys'), 'STUDIO_PORT': str(port)}
        Path(env['STUDIO_KEYS']).mkdir(parents=True)
        for k in ('admin-key', 'worker-key'):
            (Path(env['STUDIO_KEYS']) / k).write_text('secret-' + k)
        proc = subprocess.Popen([sys.executable, str(ROOT / 'server.py')], env=env)
        try:
            for _ in range(40):
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=1) as r:
                        if r.status == 200: break
                except Exception:
                    time.sleep(0.1)
            else:
                raise AssertionError('server failed to start')
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=5) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
    print('PASS server.py exits cleanly with code 0 on SIGTERM')


def test_atomic_autodeploy_and_rollback():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        origin = tmp / 'origin.git'
        work = tmp / 'work'
        work.mkdir()
        subprocess.run(['git', 'init', '--bare', '-q', str(origin)], check=True)
        subprocess.run(f'git -C {str(ROOT)!r} archive HEAD | tar -x -C {str(work)!r}', shell=True, check=True)
        subprocess.run(['git', '-C', str(work), 'init', '-b', 'main', '-q'], check=True)
        subprocess.run(['git', '-C', str(work), 'config', 'user.name', 'ci'], check=True)
        subprocess.run(['git', '-C', str(work), 'config', 'user.email', 'ci@example.com'], check=True)
        subprocess.run(['git', '-C', str(work), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(work), 'commit', '-m', 'init', '-q'], check=True)
        subprocess.run(['git', '-C', str(work), 'remote', 'add', 'origin', str(origin)], check=True)
        subprocess.run(['git', '-C', str(work), 'push', '-q', 'origin', 'main'], check=True)
        sha1 = subprocess.check_output(['git', '-C', str(work), 'rev-parse', 'HEAD'], text=True).strip()

        base = tmp / 'opt-h3'
        keys = tmp / 'etc-h3'
        data = tmp / 'var-h3'
        legacy = base / 'current'
        (legacy / 'autowake').mkdir(parents=True)
        (legacy / 'director.py').write_text("PROJECT = 'prod-vertex-999'\n")
        (legacy / 'autowake' / 'colab_rest.py').write_text("PROJECT = 'prod-colab-888'\n")

        data.mkdir(parents=True)
        with sqlite3.connect(data / 'studio.sqlite') as c:
            c.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, state TEXT)")
            c.execute("INSERT INTO jobs VALUES('j1', 'queued')")

        healthy = [True]
        port = free_port()

        class HealthHandler(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                code = 200 if healthy[0] else 503
                body = json.dumps({'ok': healthy[0]}).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        hs = HTTPServer(('127.0.0.1', port), HealthHandler)
        threading.Thread(target=hs.serve_forever, daemon=True).start()

        ctl_log = tmp / 'systemctl.log'
        mock_ctl = tmp / 'mock-systemctl'
        mock_ctl.write_text(
            "#!/usr/bin/env bash\n"
            f"echo \"$*\" >> {str(ctl_log)!r}\n"
            "if [ \"$1\" = \"list-units\" ]; then echo 'h3-autowake.service loaded active running'; fi\n"
            "if [ \"$1\" = \"show\" ]; then echo '/usr/bin/python3 /opt/h3-studio/current/autowake/controller.py'; fi\n"
        )
        mock_ctl.chmod(0o755)

        env = {
            **os.environ,
            'H3_BASE_DIR': str(base),
            'STUDIO_KEYS': str(keys),
            'STUDIO_DATA': str(data),
            'H3_REPO_URL': str(origin),
            'H3_BRANCH': 'main',
            'H3_HEALTH_URL': f'http://127.0.0.1:{port}/api/health',
            'H3_DEPLOY_LOCK': str(tmp / 'deploy.lock'),
            'H3_SYSTEMCTL': str(mock_ctl),
            'H3_PLANNING_DRAIN_SECONDS': '2',
        }
        try:
            # 1. Initial migration from legacy directory: extracts live GCP projects & switches to symlink.
            subprocess.run(['bash', str(ROOT / 'deploy' / 'auto-deploy.sh')], env=env, check=True)
            assert (keys / 'gcp-project').read_text().strip() == 'prod-vertex-999'
            assert (keys / 'colab-project').read_text().strip() == 'prod-colab-888'
            assert (base / 'current').is_symlink()
            assert (base / 'current').resolve() == (base / 'releases' / sha1).resolve()
            assert 'restart h3-studio.service' in ctl_log.read_text()
            assert 'restart h3-autowake.service' in ctl_log.read_text()
            assert (data / 'studio.sqlite').is_file()

            # 2. Repeated run with no new commit is an instant no-op.
            ctl_log.write_text('')
            subprocess.run(['bash', str(ROOT / 'deploy' / 'auto-deploy.sh')], env=env, check=True)
            assert ctl_log.read_text() == ''

            # 3. Push a new commit while health check fails -> automatic rollback to sha1!
            (work / 'README.md').write_text((work / 'README.md').read_text() + '\n')
            subprocess.run(['git', '-C', str(work), 'commit', '-am', 'next release', '-q'], check=True)
            subprocess.run(['git', '-C', str(work), 'push', '-q', 'origin', 'HEAD:refs/heads/main'], check=True)
            healthy[0] = False
            res = subprocess.run(['bash', str(ROOT / 'deploy' / 'auto-deploy.sh')], env=env)
            assert res.returncode != 0
            assert (base / 'current').resolve() == (base / 'releases' / sha1).resolve(), 'Must roll back to previous healthy release'
        finally:
            hs.shutdown()
            hs.server_close()
    print('PASS legacy project extraction, atomic symlink switch, autowake discovery, no-op fast path, and automatic rollback')


if __name__ == '__main__':
    test_project_resolution()
    test_graceful_sigterm_drain()
    test_atomic_autodeploy_and_rollback()
