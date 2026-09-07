#!/usr/bin/env python3
"""LAB ONLY: HTTP workflow, gateway restart and tenant isolation acceptance.

Uses unique test names, loopback port-forward, and cleans its own resources.
Never points at a production context. Requires the lab platform to be deployed.
"""
import copy
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time

CTX = os.environ.get('KUBE_CONTEXT', 'kind-b300-prelab')
if not CTX.startswith('kind-'):
    raise SystemExit('live_readiness.py is lab-only; use the DGX release gates for hardware')
K = ['kubectl', '--context', CTX]
GUARD = str(Path(__file__).resolve().parents[1] / 'scripts/guard.sh')
subprocess.run([GUARD, 'check'], check=True, stdout=subprocess.DEVNULL)
prefix = 'accept-' + secrets.token_hex(4)
user = prefix
password = secrets.token_urlsafe(24)
ns = 'tenant-arise'
probe_ns = 'tenant-' + prefix
internal_probe_ns = 'probe-' + prefix
port_process = None
port_log = None
port = None
admin = None


def kubectl(*args, body=None, check=True):
    result = subprocess.run([*K, *args], input=None if body is None else json.dumps(body),
                            text=True, capture_output=True, timeout=120)
    if check and result.returncode:
        raise AssertionError(result.stderr[-2000:])
    return result


def forward():
    global port_process, port_log, port
    if port_process:
        port_process.terminate()
        port_process.wait(timeout=10)
        port_log.close()
    port_log = tempfile.TemporaryFile(mode='w+')
    port_process = subprocess.Popen([*K, '-n', 'platform-system', 'port-forward',
                                     '--address=127.0.0.1', 'svc/platform-gateway', '0:8080'],
                                    stdout=port_log, stderr=port_log, text=True)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        port_log.seek(0)
        output = port_log.read()
        match = re.search(r'Forwarding from 127\.0\.0\.1:(\d+)', output)
        if match:
            port = int(match[1]); return
        if port_process.poll() is not None:
            raise AssertionError(output)
        time.sleep(.2)
    raise AssertionError('gateway port-forward timed out')


def request(method, path, body=None, cookie=None, expected=200):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
    headers = {'Content-Type': 'application/json'}
    if cookie: headers['Cookie'] = cookie
    try:
        c.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        response = c.getresponse()
        raw = response.read()
        try: data = json.loads(raw)
        except ValueError: data = raw.decode()
        assert response.status == expected, (method, path, response.status, data)
        return data, response.getheader('Set-Cookie', '').split(';')[0]
    finally:
        c.close()


def login(username, pw):
    return request('POST', '/auth/login', {'username': username, 'password': pw})[1]


def pod(selector, tolerations):
    return {'apiVersion': 'v1', 'kind': 'Pod',
            'metadata': {'name': prefix, 'namespace': probe_ns},
            'spec': {'automountServiceAccountToken': False, 'nodeSelector': selector,
                     'tolerations': tolerations,
                     'securityContext': {'runAsNonRoot': True, 'runAsUser': 65532,
                                         'seccompProfile': {'type': 'RuntimeDefault'}},
                     'containers': [{'name': 'test', 'image': 'python:3.12', 'command': ['true'],
                                     'resources': {'requests': {'cpu': '500m', 'memory': '512Mi'},
                                                   'limits': {'cpu': '500m', 'memory': '512Mi'}},
                                     'securityContext': {'allowPrivilegeEscalation': False,
                                                         'capabilities': {'drop': ['ALL']}}}]}}


try:
    forward()
    admin = login('admin', os.environ.get('GW_ADMIN_PASSWORD', 'arise-admin'))
    request('POST', '/auth/users', {'username': user, 'password': password, 'role': 'user', 'tenant': ns}, admin, 201)
    live_cookie = login(user, password)
    revoked_cookie = login(user, password)
    request('POST', '/auth/logout', {}, revoked_cookie)
    request('GET', '/auth/me', cookie=revoked_cookie, expected=401)
    print('PASS: account creation and server-side logout', flush=True)

    body = {'name': prefix, 'vcpu': 1, 'memGi': 1, 'gpu': 0}
    url = '/papi/devmachines?ns=' + ns
    request('POST', url, body, live_cookie, 201)
    request('POST', url, body, live_cookie, 201)
    request('POST', url, {**body, 'vcpu': 2}, live_cookie, 409)
    request('POST', '/papi/jobs?ns=' + ns, {'name': prefix, 'replicas': 0}, live_cookie, 400)
    request('GET', '/papi/overview?ns=tenant-direct', cookie=live_cookie, expected=403)
    request('GET', '/oapi/fleet', cookie=live_cookie, expected=403)
    kubectl('-n', ns, 'wait', 'pod/' + prefix, '--for=condition=Ready', '--timeout=60s')
    print('PASS: development machine starts, repeated creation is safe, tenant and admin fences hold', flush=True)

    # Positive and negative admission controls in a second DIRECT namespace.
    kubectl('create', '-f', '-', body={'apiVersion': 'v1', 'kind': 'Namespace',
        'metadata': {'name': probe_ns, 'labels': {'arise.ai/tier': 'tenant', 'arise.ai/owner': 'DIRECT',
            'pod-security.kubernetes.io/enforce': 'restricted'}}})
    tolerate = [{'key': 'arise.ai/direct-owned', 'operator': 'Equal', 'value': 'true', 'effect': 'NoSchedule'}]
    wrong = pod({'arise.ai/owner': 'DIRECT', 'arise.ai/tenant': 'tenant-direct'}, tolerate)
    denied = kubectl('create', '--dry-run=server', '-f', '-', body=wrong, check=False)
    assert denied.returncode != 0 and 'reserved node' in denied.stderr, denied.stderr
    omitted = pod({'arise.ai/owner': 'DIRECT'}, tolerate)
    denied = kubectl('create', '--dry-run=server', '-f', '-', body=omitted, check=False)
    assert denied.returncode != 0 and 'Dedicated workloads' in denied.stderr, denied.stderr
    own = pod({'arise.ai/owner': 'DIRECT', 'arise.ai/tenant': probe_ns}, tolerate)
    kubectl('create', '--dry-run=server', '-f', '-', body=own)
    print('PASS: second dedicated tenant may select itself; other-tenant and omitted binding denied', flush=True)

    # A non-tenant namespace with no egress rules cannot bypass the gateway.
    kubectl('create', '-f', '-', body={'apiVersion': 'v1', 'kind': 'Namespace',
        'metadata': {'name': internal_probe_ns, 'labels': {
            'pod-security.kubernetes.io/enforce': 'restricted'}}})
    targets = []
    for app in ('tenant-portal', 'ops-console'):
        result = kubectl('-n', 'platform-system', 'get', 'pods', '-l',
                         'app.kubernetes.io/name=' + app, '-o', 'json')
        targets.append(json.loads(result.stdout)['items'][0]['status']['podIP'])
    probe = pod({}, [])
    probe['metadata']['namespace'] = internal_probe_ns
    probe['spec']['restartPolicy'] = 'Never'
    probe['spec']['containers'][0]['image'] = 'python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36'
    code = "import socket,time,json\ntime.sleep(3)\nout=[]\nfor host in " + repr(targets) + ":\n try:\n  s=socket.create_connection((host,8080),2); s.close(); out.append('OPEN')\n except OSError: out.append('BLOCKED')\nprint(json.dumps(out))"
    probe['spec']['containers'][0]['command'] = ['python3', '-c', code]
    kubectl('create', '-f', '-', body=probe)
    kubectl('-n', internal_probe_ns, 'wait', 'pod/' + prefix,
            '--for=jsonpath={.status.phase}=Succeeded', '--timeout=60s')
    result = json.loads(kubectl('-n', internal_probe_ns, 'logs', prefix).stdout)
    assert result == ['BLOCKED', 'BLOCKED'], result
    request('GET', '/papi/overview?ns=' + ns, cookie=live_cookie)
    print('PASS: arbitrary non-tenant namespace blocked from both internal APIs; authenticated gateway still works', flush=True)

    kubectl('-n', 'platform-system', 'rollout', 'restart', 'deploy/platform-gateway')
    kubectl('-n', 'platform-system', 'rollout', 'status', 'deploy/platform-gateway', '--timeout=90s')
    forward()
    profile, _ = request('GET', '/auth/me', cookie=live_cookie)
    assert profile['name'] == user
    request('GET', '/auth/me', cookie=revoked_cookie, expected=401)
    login(user, password)
    print('PASS: account and valid session survive a real pod replacement; logged-out cookie remains invalid', flush=True)

    job = 'auth-backup-' + prefix
    kubectl('-n', 'platform-system', 'create', 'job', job, '--from=cronjob/auth-backup')
    try:
        kubectl('-n', 'platform-system', 'wait', 'job/' + job, '--for=condition=complete', '--timeout=90s')
        logs = kubectl('-n', 'platform-system', 'logs', 'job/' + job).stdout
        assert 'sha256' in logs and 'accounts' in logs, logs
        print('PASS: consistent backup job completed and verified its database copy', flush=True)
    finally:
        kubectl('-n', 'platform-system', 'delete', 'job', job, '--ignore-not-found', check=False)
finally:
    try:
        if port:
            admin = login('admin', os.environ.get('GW_ADMIN_PASSWORD', 'arise-admin'))
            request('DELETE', '/auth/users/' + user, cookie=admin)
    except Exception as exc:
        print('Cleanup warning (test account):', type(exc).__name__, flush=True)
    kubectl('-n', ns, 'delete', 'pod', prefix, '--ignore-not-found', '--wait=false', check=False)
    kubectl('delete', 'namespace', probe_ns, internal_probe_ns, '--ignore-not-found', '--wait=false', check=False)
    if port_process:
        port_process.terminate(); port_process.wait(timeout=10); port_log.close()

    subprocess.run([GUARD, "check"], check=True, stdout=subprocess.DEVNULL)
