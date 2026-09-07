#!/usr/bin/env python3
"""Bounded, read-only lab API load sample. This is not a sustained-load SLO test."""
import concurrent.futures
import http.client
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
CTX = os.environ.get('KUBE_CONTEXT', 'kind-b300-prelab')
if not CTX.startswith('kind-'):
    raise SystemExit('Read load acceptance is kind-only')
K = ['kubectl', '--context', CTX]
GUARD = ['bash', str(ROOT / 'scripts/guard.sh'), 'check']
subprocess.run(GUARD, check=True, stdout=subprocess.DEVNULL)


def main():
    with tempfile.TemporaryFile(mode='w+') as log:
        p = subprocess.Popen([*K, '-n', 'platform-system', 'port-forward', '--address=127.0.0.1',
                              'svc/platform-gateway', '0:8080'], stdout=log, stderr=log,
                             env={**os.environ, 'KUBECTL_PORT_FORWARD_WEBSOCKETS': 'false'})
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                log.seek(0)
                m = re.search(r'127\.0\.0\.1:(\d+)', log.read())
                if m:
                    port = int(m[1]); break
                if p.poll() is not None:
                    raise RuntimeError('lab gateway port-forward failed')
                time.sleep(.05)
            else:
                raise RuntimeError('port-forward timeout')

            def request(path, method='GET', body=None, cookie=None):
                start = time.monotonic()
                c = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
                try:
                    headers = {'Content-Type': 'application/json', 'Connection': 'close'}
                    if cookie: headers['Cookie'] = cookie
                    c.request(method, path, None if body is None else json.dumps(body), headers)
                    r = c.getresponse()
                    payload = json.loads(r.read())
                    return r.status, payload, r.getheader('Set-Cookie', '').split(';')[0], time.monotonic() - start
                finally:
                    c.close()

            status, _, cookie, _ = request('/auth/login', 'POST', {
                'username': 'admin', 'password': os.environ.get('GW_ADMIN_PASSWORD', 'arise-admin')})
            assert status == 200, 'fixture authentication failed'
            paths = ['/auth/me', '/papi/overview?ns=tenant-arise',
                     '/papi/usage?ns=tenant-arise', '/oapi/fleet'] * 20

            def sample(path):
                try:
                    status, _, _, seconds = request(path, cookie=cookie)
                    return {'path': path, 'status': status, 'seconds': seconds}
                except (OSError, http.client.HTTPException, ValueError) as e:
                    return {'path': path, 'status': 'ERROR', 'error': type(e).__name__}

            start = time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(sample, paths))
            elapsed = time.monotonic() - start
            failures = [r for r in results if r['status'] != 200]
            durations = sorted(r['seconds'] for r in results if 'seconds' in r)
            ready = request('/readyz')[0]
            report = {'context': CTX, 'workers': 4, 'requests': len(results),
                      'elapsedSeconds': round(elapsed, 3), 'errors': failures,
                      'p50Seconds': round(durations[math.ceil(len(durations) * .5) - 1], 3) if durations else None,
                      'p95Seconds': round(durations[math.ceil(len(durations) * .95) - 1], 3) if durations else None,
                      'maxSeconds': round(max(durations), 3) if durations else None,
                      'readyAfter': ready, 'scope': 'short read-only sample through loopback kubectl tunnel',
                      'status': 'PASS' if not failures and ready == 200 else 'FAIL'}
            print(json.dumps(report, indent=2))
            assert report['status'] == 'PASS', 'read requests failed or service did not recover'
        finally:
            p.terminate(); p.wait(timeout=10)
            subprocess.run(GUARD, check=True, stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
