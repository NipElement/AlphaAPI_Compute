#!/usr/bin/env python3
"""LAB ONLY: real Calico path, tenant SSH, private services and access revocation.

Refuses an already-enabled bastion. Uses disposable customer keys and workloads,
leaves the optional entry disabled with an empty key register, retains host PVC.
No production context, public listener, customer credential or GPU is needed.
"""
import http.client
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import select
import shlex
import subprocess
import tempfile
import time
import urllib.request
import yaml

ROOT = Path(__file__).resolve().parents[1]
CTX = os.environ.get('KUBE_CONTEXT', 'kind-b300-prelab')
if not CTX.startswith('kind-'):
    raise SystemExit('This acceptance test only accepts a kind context')
K = ['kubectl', '--context', CTX]


def run(args, **kw):
    r = subprocess.run(args, capture_output=True, text=True, timeout=kw.pop('timeout', 120), **kw)
    if r.returncode:
        raise AssertionError(f'{args[:4]}: {r.stderr[-1800:]}')
    return r.stdout.strip()


def main():
    run([str(ROOT/'scripts/guard.sh'), 'check'])
    existing = run(K + ['-n', 'access-system', 'get', 'deploy', 'tenant-bastion', '--ignore-not-found', '-o', 'json'])
    if existing and json.loads(existing)['spec'].get('replicas', 1):
        raise SystemExit('Refusing to replace an enabled access gateway')
    existing_keys = run(K + ['-n', 'access-system', 'get', 'cm', 'tenant-bastion-access-keys', '--ignore-not-found', '-o', 'json'])
    if existing_keys and json.loads(existing_keys).get('data', {}).get('authorized_keys'):
        raise SystemExit('Refusing to replace registered access keys')
    prefix = 'access-' + secrets.token_hex(4)
    tenants = ('tenant-arise', 'tenant-direct')
    processes, logs = [], []
    enabled = False
    try:
        with tempfile.TemporaryDirectory(prefix='alphaapi-cluster-access-') as tmp:
            tmp = Path(tmp)
            def forward(namespace, service, destination):
                log = tempfile.TemporaryFile(mode='w+'); logs.append(log)
                p = subprocess.Popen(K + ['-n', namespace, 'port-forward', '--address=127.0.0.1',
                    'svc/' + service, '0:' + str(destination)], stdout=log, stderr=log, text=True)
                processes.append(p)
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    log.seek(0); output = log.read()
                    match = re.search(r'Forwarding from 127\.0\.0\.1:(\d+)', output)
                    if match:
                        return int(match[1])
                    if p.poll() is not None:
                        raise AssertionError(output)
                    time.sleep(.2)
                raise AssertionError('port-forward did not start')
            gateway = forward('platform-system', 'platform-gateway', 8080)
            def request(method, path, body=None, cookie=None, expected=200):
                conn = http.client.HTTPConnection('127.0.0.1', gateway, timeout=30)
                try:
                    conn.request(method, path, body=None if body is None else json.dumps(body),
                                 headers={'Content-Type': 'application/json', 'Cookie': cookie or ''})
                    response = conn.getresponse(); raw = response.read()
                    assert response.status == expected, (path, response.status, raw[:1000])
                    return json.loads(raw), response.getheader('Set-Cookie', '').split(';')[0]
                finally:
                    conn.close()
            admin = request('POST', '/auth/login', {'username': 'admin',
                            'password': os.environ.get('GW_ADMIN_PASSWORD', 'arise-admin')})[1]
            keys, entries = [], []
            for n, tenant in enumerate(tenants):
                key = tmp/f'key-{n}'; keys.append(key)
                run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)])
                public = key.with_suffix('.pub').read_text().strip()
                entries.append({'tenant': tenant, 'publicKey': public})
                request('POST', '/papi/devmachines?ns=' + tenant, {
                    'name': prefix, 'vcpu': 1, 'memGi': 1, 'gpu': 0, 'sshPublicKey': public}, admin, 201)
                run(K + ['-n', tenant, 'wait', 'pod/' + prefix, '--for=condition=Ready', '--timeout=90s'])
            request('POST', '/papi/services?ns=' + tenants[0], {'name': prefix, 'vcpu': 1, 'memGi': 1, 'gpu': 0,
                'script': "from http.server import BaseHTTPRequestHandler,HTTPServer\nclass H(BaseHTTPRequestHandler):\n def do_GET(self):\n  self.send_response(200); self.end_headers(); self.wfile.write(b'private-tenant-service')\nHTTPServer(('0.0.0.0',8080),H).serve_forever()"}, admin, 201)
            run(K + ['-n', tenants[0], 'rollout', 'status', 'deploy/' + prefix, '--timeout=90s'])
            config = tmp/'keys.yaml'
            def deploy(selected):
                config.write_text(yaml.safe_dump({'keys': selected}))
                run(['bash', str(ROOT/'scripts/deploy-access.sh'), 'lab', CTX, str(config)], timeout=240)
            enabled = True
            deploy(entries)
            run(['python3', str(ROOT/'scripts/verify-access.py'), '--context', CTX, '--overlay', 'lab', '--keys', str(config)])
            port = forward('access-system', 'tenant-bastion', 2222)
            def hostkey():
                return run(K + ['-n', 'access-system', 'exec', 'deploy/tenant-bastion', '--',
                                 'ssh-keygen', '-y', '-f', '/keys/ssh_host_ed25519_key'])
            host = hostkey()
            known = tmp/'known_hosts'
            known.write_text(f'[127.0.0.1]:{port} {host}\n')
            for tenant in tenants:
                pub = run(K + ['-n', tenant, 'exec', prefix, '--', 'ssh-keygen', '-y', '-f', '/keys/ssh_host_ed25519_key'])
                with known.open('a') as f:
                    f.write(f'{prefix}.{tenant} {pub}\n')
            def outer(n=0):
                return ['ssh', '-T', '-p', str(port), '-i', str(keys[n]), '-o', 'BatchMode=yes',
                        '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=5',
                        '-o', 'UserKnownHostsFile=' + str(known), 'dev@127.0.0.1']
            def inner(n=0):
                return ['ssh', '-T', '-i', str(keys[n]), '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                        '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(known),
                        '-o', 'HostKeyAlias=' + prefix + '.' + tenants[n],
                        '-o', 'ProxyCommand=' + shlex.join(['python3', str(ROOT/'services/ssh-bastion/client.py'),
                            'proxy', prefix, '--host', '127.0.0.1', '--port', str(port), '--key', str(keys[n]),
                            '--known-hosts', str(known)]), 'dev@box']
            for n in (0, 1):
                assert run(inner(n) + ['id -u']) == '65532'
            print('PASS: real cluster routes both registered tenants through their own SSH Service with pinned host identities', flush=True)
            payload = secrets.token_hex(200000)
            assert run(inner() + ['cat > /home/dev/probe; cat /home/dev/probe'], input=payload) == payload
            upload = tmp/'upload.bin'; upload.write_bytes(os.urandom(1024 * 1024))
            run(['scp', *inner()[2:-1], str(upload), 'dev@box:/home/dev/upload.bin'])
            assert run(inner() + ['sha256sum /home/dev/upload.bin']).split()[0] == hashlib.sha256(upload.read_bytes()).hexdigest()
            for command in (prefix + '.tenant-direct', 'service:' + prefix + '.tenant-direct', '127.0.0.1', 'id'):
                r = subprocess.run(outer() + [command], capture_output=True, text=True, timeout=20)
                assert r.returncode != 0 and not r.stdout, (command, r.stdout)
            print('PASS: standalone client SSH and binary scp transfer succeed; cross-tenant and arbitrary targets are denied', flush=True)
            client = subprocess.Popen(['python3', str(ROOT/'services/ssh-bastion/client.py'), 'service', prefix,
                '--host', '127.0.0.1', '--port', str(port), '--key', str(keys[0]), '--known-hosts', str(known),
                '--local-port', '0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(client)
            assert select.select([client.stdout], [], [], 10)[0]
            local_port = int(re.search(r'127\.0\.0\.1:(\d+)', client.stdout.readline())[1])
            with urllib.request.urlopen(f'http://127.0.0.1:{local_port}', timeout=20) as response:
                assert response.read() == b'private-tenant-service'
            client.terminate(); client.wait(timeout=5)
            print('PASS: customer CLI reaches private HTTP service over actual Calico policies', flush=True)
            target_ips = []
            for app in ('platform-gateway', 'tenant-portal', 'ops-console'):
                pods = json.loads(run(K + ['-n', 'platform-system', 'get', 'pods', '-l',
                    'app.kubernetes.io/name=' + app, '-o', 'json']))
                target_ips.append(pods['items'][0]['status']['podIP'])
            probe = "import socket,json\nout=[]\nfor host in " + repr(target_ips) + ":\n try:\n  s=socket.create_connection((host,8080),2); s.close(); out.append('OPEN')\n except OSError: out.append('BLOCKED')\nprint(json.dumps(out))"
            result = run(K + ['-n', 'access-system', 'exec', 'deploy/tenant-bastion', '--', 'python3', '-c', probe])
            assert json.loads(result) == ['BLOCKED'] * 3, result
            run(K + ['-n', 'access-system', 'exec', 'deploy/tenant-bastion', '--', 'test', '!', '-e',
                     '/var/run/secrets/kubernetes.io/serviceaccount/token'])
            print('PASS: bastion cannot connect to platform APIs and has no Kubernetes credential', flush=True)
            active = subprocess.Popen(inner() + ['sleep 120'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(active); time.sleep(2)
            assert active.poll() is None
            deploy(entries[1:])
            active.wait(timeout=20)
            assert hostkey() == host
            port = forward('access-system', 'tenant-bastion', 2222)
            with known.open('a') as f:
                f.write(f'[127.0.0.1]:{port} {host}\n')
            refused = subprocess.run(outer() + [prefix], capture_output=True, text=True, timeout=15)
            assert refused.returncode != 0 and 'Permission denied' in refused.stderr
            assert run(inner(1) + ['id -u']) == '65532'
            print('PASS: key revocation closes active tunnels, denies new access, preserves host key and keeps other tenant usable', flush=True)
    except Exception:
        if enabled:
            logs_result = subprocess.run(K + ['-n', 'access-system', 'logs', 'deploy/tenant-bastion', '--tail=30'], capture_output=True, text=True)
            print(logs_result.stdout, logs_result.stderr, flush=True)
        raise
    finally:
        for p in reversed(processes):
            if p.poll() is None:
                p.terminate()
                try: p.wait(timeout=8)
                except subprocess.TimeoutExpired: p.kill(); p.wait()
        for log in logs:
            log.close()
        if enabled:
            with tempfile.TemporaryDirectory(prefix='alphaapi-access-revoke-') as cleanup:
                empty = Path(cleanup)/'keys.yaml'
                empty.write_text('keys: []\n')
                run(['bash', str(ROOT/'scripts/deploy-access.sh'), 'lab', CTX, str(empty)], timeout=180)
            final = json.loads(run(K + ['-n', 'access-system', 'get', 'cm', 'tenant-bastion-access-keys', '-o', 'json']))
            assert not final['data']['authorized_keys']
            print('PASS: removing the last key disables access and clears the active key register', flush=True)
        for tenant in tenants:
            run(K + ['-n', tenant, 'delete', 'pod,deploy,svc', prefix, '--ignore-not-found', '--wait=false'])
            run(K + ['-n', tenant, 'delete', 'svc,cm', prefix + '-ssh', '--ignore-not-found', '--wait=false'])
        run([str(ROOT/'scripts/guard.sh'), 'check'])


if __name__ == '__main__':
    main()
