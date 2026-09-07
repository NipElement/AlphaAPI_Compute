#!/usr/bin/env python3
"""Offline TLS/proxy integration against the pinned Caddy image on loopback only."""
import http.server
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Upstream(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        data = json.dumps({'xff': self.headers.get('X-Forwarded-For'),
                           'proto': self.headers.get('X-Forwarded-Proto')}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Set-Cookie', '__Host-test=ok; Path=/; Secure; HttpOnly; SameSite=Lax')
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        expected = int(self.headers.get('Content-Length', 0))
        if len(self.rfile.read(expected)) == expected:
            self.do_GET()

    def log_message(self, *_):
        pass


def main():
    upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    name = 'alphaapi-edge-test-' + uuid.uuid4().hex[:10]
    port, health = free_port(), free_port()
    pinned = next(s.split('=', 1)[1].split('#')[0].strip() for s in
                  (ROOT/'versions.env').read_text().splitlines() if s.startswith('CADDY_IMAGE='))
    try:
        with tempfile.TemporaryDirectory(prefix='alphaapi-edge-') as tmp:
            path = Path(tmp)
            path.chmod(0o755)
            for d in ('data', 'runtime'):
                (path/d).mkdir(mode=0o777)
                (path/d).chmod(0o777)  # disposable test dirs, equivalent to PVC fsGroup
            config = (ROOT/'platform/overlays/dgx/edge/Caddyfile').read_text()
            config = config.replace('admin off', 'admin off\n\tskip_install_trust\n\tauto_https disable_redirects')
            config = config.replace('{$CONSOLE_FQDN} {', f'localhost:{port} {{\n\tbind 127.0.0.1\n\ttls internal')
            config = config.replace('platform-gateway.platform-system.svc.cluster.local:8080',
                                    '127.0.0.1:' + str(upstream.server_port))
            config = config.replace('127.0.0.1:8081', f'127.0.0.1:{health}')
            (path/'Caddyfile').write_text(config)
            subprocess.run(['docker', 'run', '-d', '--name', name, '--network', 'host',
                            '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop', 'ALL', '--cap-add', 'NET_BIND_SERVICE',
                            '--security-opt', 'no-new-privileges', '--read-only',
                            '-e', 'ACME_EMAIL=ops@example.invalid',
                            '-v', f'{path}/Caddyfile:/etc/caddy/Caddyfile:ro',
                            '-v', f'{path}/data:/data', '-v', f'{path}/runtime:/config',
                            pinned], check=True, stdout=subprocess.DEVNULL)
            ca = path/'data/caddy/pki/authorities/local/root.crt'
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{health}/healthz', timeout=1) as res:
                        assert res.status == 200
                    if ca.exists():
                        break
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(.2)
            else:
                raise RuntimeError('edge did not become healthy')
            context = ssl.create_default_context(cafile=str(ca))
            url = f'https://localhost:{port}/auth/me'
            req = urllib.request.Request(url, headers={'X-Forwarded-For': '203.0.113.88',
                                                       'X-Forwarded-Proto': 'http'})
            with urllib.request.urlopen(req, context=context, timeout=5) as res:
                body = json.load(res)
                assert body == {'xff': '127.0.0.1', 'proto': 'https'}, body
                assert 'Secure' in res.headers['Set-Cookie']
                assert not res.headers.get('Server'), dict(res.headers)
            print('PASS: trusted TLS certificate, nonroot read-only runtime, spoofed headers replaced, secure cookie preserved')
            # An oversized request must fail at the edge, before becoming an API mutation.
            req = urllib.request.Request(url, data=b'x' * (1024 * 1024 + 1), method='POST')
            try:
                urllib.request.urlopen(req, context=context, timeout=8)
                raise AssertionError('oversized request accepted')
            except urllib.error.HTTPError as e:
                assert e.code == 413, e.code
            print('PASS: oversized request rejected with 413')
    except Exception:
        subprocess.run(['docker', 'logs', '--tail', '25', name], check=False)
        raise
    finally:
        subprocess.run(['docker', 'rm', '-f', name], check=False, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        upstream.shutdown()
        upstream.server_close()


if __name__ == '__main__':
    main()
