"""Behavior regressions for persistent identity, provisioning and invoices.

No Kubernetes cluster/GPU required. HTTP cases use real loopback sockets;
provisioning exercises a deterministic Kubernetes API double with conflicts,
response loss and UID preconditions. The existing matrix supplies live API tests.
"""
import base64
import copy
import concurrent.futures
import csv
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
from contextlib import redirect_stdout, closing
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(rel):
    spec = importlib.util.spec_from_file_location(rel.replace('/', '_'), ROOT / rel)
    out = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(out)
    return out


def close_store(gw):
    if gw.AUTH_DB is not None:
        gw.AUTH_DB.close()
    if gw.AUTH_DB_LOCK is not None:
        gw.AUTH_DB_LOCK.close()


class PublicKeyTests(unittest.TestCase):
    def test_key_validation_rejects_truncated_mismatched_and_wrong_sized_payloads(self):
        portal = module('services/tenant-portal/tenant_portal.py')
        pack = lambda value: struct.pack('>I', len(value)) + value
        payload = pack(b'ssh-ed25519') + pack(bytes(range(32)))
        valid = 'ssh-ed25519 ' + base64.b64encode(payload).decode()
        self.assertEqual(portal.validate_ssh_public_key(valid), valid)
        for bad in (None, [], 42, 'ssh-ed25519 AAAA', 'ssh-rsa ' + valid.split()[1],
                    'ssh-ed25519 ' + base64.b64encode(payload[:-1]).decode(),
                    'ssh-ed25519 ' + base64.b64encode(payload + b'trailing').decode(),
                    'ssh-ed25519 ' + base64.b64encode(pack(b'ssh-ed25519') + pack(b'short')).decode(),
                    'command="id" ' + valid, valid + '\n' + valid):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(portal.ApiError):
                    portal.validate_ssh_public_key(bad)

    def test_real_openssh_ed25519_rsa_and_ecdsa_keys_are_accepted(self):
        portal = module('services/tenant-portal/tenant_portal.py')
        with tempfile.TemporaryDirectory(prefix='alphaapi-key-validation-') as directory:
            for algorithm in ('ed25519', 'rsa', 'ecdsa'):
                path = Path(directory) / algorithm
                subprocess.run(['ssh-keygen', '-q', '-t', algorithm, '-N', '', '-f', str(path)], check=True, timeout=15)
                key = path.with_suffix('.pub').read_text().strip()
                self.assertEqual(portal.validate_ssh_public_key(key), key)


class FleetReadTests(unittest.TestCase):
    def setUp(self):
        self.ops = module('services/ops-console/console.py')
        self.ops.log = lambda *args, **kwargs: None

    def test_native_resources_are_normalized_in_the_fleet_response(self):
        ops = self.ops
        ops.FAKE_GPU = 'nvidia.com/gpu'
        node = {'metadata': {'name': 'worker', 'labels': {ops.NODE_ID_LABEL: 'dgx01'}},
                'status': {'allocatable': {'cpu': '287500m', 'memory': '2250Gi', 'nvidia.com/gpu': '8'}}}
        pod = {'metadata': {'namespace': 'tenant-arise', 'name': 'work'},
               'spec': {'nodeName': 'worker', 'containers': [
                   {'resources': {'requests': {'cpu': '1500m', 'memory': '1610612736'}}}],
                   'initContainers': [{'resources': {'requests': {
                       'cpu': '2', 'memory': '2Gi', 'nvidia.com/gpu': '1'}}}]},
               'status': {'phase': 'Running'}}
        def api(method, path):
            if '/nodes?' in path: return {'items': [node]}
            if path == '/api/v1/pods': return {'items': [pod]}
            return {'items': []}
        with patch.object(ops, 'api', api), patch.object(ops, 'tenant_namespaces_now', return_value=('tenant-arise',)):
            result = ops.build_fleet()[0]
        self.assertEqual((result['gpuTotal'], result['gpuUsed']), (8, 1))
        self.assertEqual((result['vcpuTotal'], result['vcpuUsed']), (287.5, 2))
        self.assertEqual((result['memGiTotal'], result['memGiUsed']), (2250, 2))
        self.assertNotIn('simVcpuTotal', result)

    def test_lab_resources_take_precedence_over_container_overhead(self):
        self.assertEqual(self.ops._resources({
            self.ops.SIM_VCPU: '32', self.ops.SIM_MEM: '256',
            'cpu': '500m', 'memory': '512Mi'}), {'gpu': 0, 'vcpu': 32, 'memGi': 256})
        self.assertEqual(self.ops._resources({'cpu': '500m', 'memory': '512Mi'}),
                         {'gpu': 0, 'vcpu': 0, 'memGi': 0})
        self.ops.FAKE_GPU = 'nvidia.com/gpu'
        self.assertEqual(self.ops._resources({'cpu': '1500m', 'memory': '1610612736'}),
                         {'gpu': 0, 'vcpu': 1.5, 'memGi': 1.5})

    def test_ownership_read_failure_cannot_become_zero_contracts(self):
        def api(method, path):
            if '/nodes?' in path:
                return {'items': []}
            raise urllib.error.HTTPError(path, 503, 'unavailable', {}, io.BytesIO())
        with patch.object(self.ops, 'api', api):
            with self.assertRaises(urllib.error.HTTPError):
                self.ops.build_fleet()

    def test_optional_failures_are_explicit_and_keep_fleet_available(self):
        with patch.object(self.ops, 'build_fleet', return_value=[]), \
             patch.object(self.ops, 'build_infra', return_value=[{'name': 'head'}]), \
             patch.object(self.ops.urllib.request, 'urlopen', side_effect=urllib.error.URLError('offline')), \
             patch.object(self.ops, 'api', side_effect=urllib.error.HTTPError('events', 403, 'forbidden', {}, io.BytesIO())):
            response = self.ops.fleet_response()
        self.assertEqual(response['infraNodes'], [{'name': 'head'}])
        self.assertEqual(response['errors'], {'alerts': 'unavailable', 'events': 'unavailable'})

    def test_successful_empty_results_are_not_an_outage(self):
        with patch.object(self.ops, 'build_fleet', return_value=[]), \
             patch.object(self.ops, 'build_infra', return_value=[]), \
             patch.object(self.ops, 'active_alerts', return_value=[]), \
             patch.object(self.ops, 'recent_events', return_value=[]):
            response = self.ops.fleet_response()
        self.assertEqual(response['errors'], {})
        self.assertEqual(response['alerts'], [])
        self.assertEqual(response['events'], [])


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + '/auth.sqlite3'
        self.gw = self.open()

    def open(self):
        with patch.dict(os.environ, {'GW_PUBLIC_MODE': 'false', 'GW_COOKIE_SECURE': 'false'}, clear=False):
            gw = module('services/gateway/gateway.py')
        gw.log = lambda *a, **k: None
        gw.init_auth_store(self.path)
        gw.SESSION_KEY = b'x' * 48
        gw.add_user('customer', 'original-password-1', 'user', 'tenant-direct', 'Customer', create_only=True) if 'customer' not in gw.USERS else None
        return gw

    def tearDown(self):
        close_store(self.gw)
        self.tmp.cleanup()

    def restart(self):
        close_store(self.gw)
        self.gw = self.open()

    def test_account_and_live_session_survive_restart(self):
        token = self.gw.new_session('customer')
        self.restart()
        self.assertTrue(self.gw.check_login('customer', 'original-password-1'))
        self.assertIsNotNone(self.gw.read_token(token))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_logout_remains_revoked_after_restart(self):
        token = self.gw.new_session('customer')
        payload = self.gw.read_token(token)
        self.gw.revoke(payload['jti'], payload['exp'], 'customer')
        self.restart()
        self.assertIsNone(self.gw.read_token(token))

    def test_logout_overflow_never_resurrects_a_cookie(self):
        tokens = []
        for _ in range(self.gw.REVOKED_PER_USER + 5):
            tok = self.gw.new_session('customer')
            p = self.gw.read_token(tok)
            self.gw.revoke(p['jti'], p['exp'], 'customer')
            tokens.append(tok)
        self.restart()
        self.assertTrue(all(self.gw.read_token(tok) is None for tok in tokens))

    def test_seed_version_rotation_survives_reseeding(self):
        self.gw.add_user('admin', 'provisioned-password', 'admin', None, 'A', deterministic=True)
        token = self.gw.new_session('admin')
        for i in range(66):
            self.gw.revoke(f'fake-{i}', time.time() + 600, 'admin')
        self.restart()
        self.gw.add_user('admin', 'provisioned-password', 'admin', None, 'A', deterministic=True)
        self.assertIsNone(self.gw.read_token(token))

    def test_password_change_and_delete_persist(self):
        token = self.gw.new_session('customer')
        self.gw.add_user('customer', 'replacement-password', 'user', 'tenant-direct', 'C')
        self.restart()
        self.assertFalse(self.gw.check_login('customer', 'original-password-1'))
        self.assertTrue(self.gw.check_login('customer', 'replacement-password'))
        self.assertIsNone(self.gw.read_token(token))
        self.gw.add_user('extra', 'replacement-password', 'user', 'tenant-direct', 'C')
        token = self.gw.new_session('extra')
        self.gw.USERS.pop('extra')
        self.restart()
        self.assertNotIn('extra', self.gw.USERS)
        self.assertIsNone(self.gw.read_token(token))

    def test_competing_writer_refused(self):
        other = module('services/gateway/gateway.py')
        with self.assertRaises(BlockingIOError):
            other.init_auth_store(self.path)

    def test_public_mode_requires_persistent_store(self):
        self.gw.PUBLIC_MODE = True
        with self.assertRaises(self.gw.SecretsMissing):
            self.gw.init_auth_store('')

    def test_live_throttle_keys_have_a_hard_bound(self):
        th = self.gw.LoginThrottle(window=60, max_fails=8, lockout=90)
        th.MAX_KEYS = 16
        for i in range(1000):
            th.record_failure((str(i),))
        self.assertLessEqual(len(th._fails) + len(th._until), 16)
        self.assertGreater(th.retry_after(('new-key',)), 0)

    def test_create_only_does_not_overwrite_credentials(self):
        with self.assertRaises(ValueError):
            self.gw.add_user('customer', 'attacker-password', 'admin', None, 'X', create_only=True)
        self.assertEqual(self.gw.USERS['customer']['role'], 'user')
        self.assertTrue(self.gw.check_login('customer', 'original-password-1'))

    def test_online_backup_is_consistent_and_restorable(self):
        import sqlite3
        dest = self.tmp.name + '/backup.sqlite3'
        with sqlite3.connect(dest) as db:
            self.gw.AUTH_DB.backup(db)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute("SELECT count(*) FROM auth_state WHERE kind='users'").fetchone()[0], 1)

    def test_invalid_register_is_not_replaced_by_defaults(self):
        gw = self.gw
        gw.TENANTS_PATH = self.tmp.name + '/tenants.json'
        Path(gw.TENANTS_PATH).write_text('[]')
        with self.assertRaises(gw.SecretsMissing):
            gw.load_tenants()
        portal = module('services/tenant-portal/tenant_portal.py')
        portal.TENANTS_PATH = gw.TENANTS_PATH
        portal.log = lambda *a, **k: None
        with self.assertRaises(RuntimeError):
            portal._load_tenants()

    def test_readiness_fails_when_identity_store_is_unavailable(self):
        gw = self.gw
        gw.WEB_DIR = self.tmp.name
        Path(gw.WEB_DIR, 'index.html').write_text('test')
        server = gw.BoundedThreadingHTTPServer(('127.0.0.1', 0), gw.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def get(path):
            c = http.client.HTTPConnection(*server.server_address, timeout=2)
            c.request('GET', path)
            r = c.getresponse()
            status = r.status
            r.read(); c.close()
            return status
        try:
            self.assertEqual(get('/readyz'), 200)
            gw.AUTH_DB.close()
            self.assertEqual(get('/readyz'), 503)
            self.assertEqual(get('/healthz'), 200)
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_real_http_rejects_bad_bodies_and_conflicting_lengths(self):
        gw = self.gw
        gw.Handler.protocol_version = 'HTTP/1.1'
        gw.Handler.timeout = 2
        server = gw.BoundedThreadingHTTPServer(('127.0.0.1', 0), gw.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for body in ('[]', 'null', '{broken', '"a string"'):
                c = http.client.HTTPConnection(*server.server_address, timeout=2)
                c.request('POST', '/auth/login', body=body, headers={'Content-Type': 'application/json'})
                response = c.getresponse()
                self.assertEqual(response.status, 400, body)
                response.read()
                c.close()
            for headers in ('Content-Length: -1', 'Content-Length: 2\r\nContent-Length: 3', 'Content-Length: nope'):
                with socket.create_connection(server.server_address, timeout=2) as c:
                    c.sendall(('POST /auth/login HTTP/1.1\r\nHost: localhost\r\n' + headers + '\r\n\r\n{}').encode())
                    self.assertIn(b' 400 ', c.recv(4096).split(b'\r\n')[0])
            c = http.client.HTTPConnection(*server.server_address, timeout=2)
            c.request('POST', '/', body='{}')
            response = c.getresponse()
            self.assertEqual(response.status, 401)
            response.read(); c.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


    def _blocked_password_request(self, path, body, during, expected):
        """Hold real HTTP password work while another operation uses identity."""
        gw = self.gw
        token = gw.new_session('customer')
        server = gw.BoundedThreadingHTTPServer(('127.0.0.1', 0), gw.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        entered, release = threading.Event(), threading.Event()
        original_hash = gw._pw_hash
        held_password = body.get('new') or body['password']
        def slow_hash(password, *args):
            if password == held_password:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test did not release password work')
            return original_hash(password, *args)
        def request(method, route, data=None):
            with closing(http.client.HTTPConnection(*server.server_address, timeout=2)) as conn:
                conn.request(method, route, body=json.dumps(data) if data else None,
                    headers={'Cookie': gw.COOKIE + '=' + token, 'Content-Type': 'application/json'})
                response = conn.getresponse()
                response.read()
                return response.status
        try:
            with patch.object(gw, '_pw_hash', slow_hash), concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(request, 'POST', path, body)
                try:
                    self.assertTrue(entered.wait(2), 'HTTP request did not reach password work')
                    during(request)
                finally:
                    release.set()
                self.assertEqual(pending.result(timeout=3), expected)
        finally:
            release.set()
            server.shutdown(); server.server_close(); thread.join(2)

    def test_password_work_does_not_block_existing_sessions(self):
        for path, body in (
            ('/auth/login', {'username': 'customer', 'password': 'original-password-1'}),
            ('/auth/password', {'current': 'original-password-1', 'new': 'replacement-password-2'}),
        ):
            with self.subTest(path=path):
                self._blocked_password_request(path, body,
                    lambda request: self.assertEqual(request('GET', '/auth/me'), 200), 200)

    def test_login_cannot_mint_a_token_for_a_replaced_identity(self):
        def replace_identity(_request):
            with self.gw._STATE_LOCK:
                old = self.gw.USERS.pop('customer')
                self.gw.USERS['customer'] = dict(old, ver='replacement-identity')
        self._blocked_password_request('/auth/login',
            {'username': 'customer', 'password': 'original-password-1'}, replace_identity, 401)
        self.assertIsNotNone(self.gw.authenticate('customer', 'original-password-1'))

    def test_password_change_cannot_overwrite_a_replaced_identity(self):
        def replace_identity(_request):
            with self.gw._STATE_LOCK:
                self.gw.USERS['customer'] = dict(self.gw.USERS['customer'], ver='replacement-identity')
        self._blocked_password_request('/auth/password',
            {'current': 'original-password-1', 'new': 'replacement-password-2'}, replace_identity, 409)
        self.assertTrue(self.gw.check_login('customer', 'original-password-1'))
        self.assertIsNone(self.gw.check_login('customer', 'replacement-password-2'))

    def test_pruning_live_revocations_does_not_rewrite_unchanged_records(self):
        self.gw.revoke('live-token', time.time() + 1000, 'customer')
        writes = self.gw.AUTH_DB.total_changes
        self.gw._prune_revoked(time.time())
        self.assertEqual(self.gw.AUTH_DB.total_changes, writes)


class InstanceReadTests(unittest.TestCase):
    def test_only_missing_pods_become_an_empty_instance_list(self):
        portal = module('services/tenant-portal/tenant_portal.py')
        for code in (403, 404, 500, 503):
            def api(method, path):
                if '?labelSelector=' in path:
                    return {'items': []}
                raise urllib.error.HTTPError('http://kube', code, 'injected', {},
                    io.BytesIO(json.dumps({'message': 'injected'}).encode()))
            with self.subTest(code=code), patch.object(portal, 'api', api):
                if code == 404:
                    self.assertEqual(portal.instances('tenant-arise', 'example'), {'instances': []})
                else:
                    with self.assertRaises(portal.ApiError) as failure:
                        portal.instances('tenant-arise', 'example')
                    self.assertEqual(failure.exception.code, code)


class FakeKube:
    def __init__(self):
        self.objects = {}
        self.fail = None
        self.lose_response = None
        self.serial = 0
        self.calls = []

    def error(self, code):
        raise urllib.error.HTTPError('http://kube', code, 'injected', {}, io.BytesIO(json.dumps({'message': 'injected'}).encode()))

    def __call__(self, method, path, body=None, **kw):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == 'GET' and path.endswith('resourcequotas'):
            return {'items': []}
        if path == self.fail:
            self.fail = None
            self.error(503)
        if method == 'POST':
            if '?dryRun=All' in path:
                if path.split('?')[0] + '/' + body['metadata'].get('name', '') in self.objects:
                    self.error(409)
                return copy.deepcopy(body)
            key = path + '/' + body['metadata']['name']
            if key in self.objects:
                self.error(409)
            self.serial += 1
            obj = copy.deepcopy(body)
            obj['metadata'].update(uid=f'uid-{self.serial}', resourceVersion=str(self.serial))
            self.objects[key] = obj
            if path == self.lose_response:
                self.lose_response = None
                raise urllib.error.URLError('response lost after commit')
            return copy.deepcopy(obj)
        if path not in self.objects:
            self.error(404)
        obj = self.objects[path]
        if method == 'GET':
            return copy.deepcopy(obj)
        if method == 'PATCH':
            if body.get('metadata', {}).get('uid') != obj['metadata']['uid']:
                self.error(409)
            for key, value in body.get('spec', {}).items():
                if value is None:
                    obj['spec'].pop(key, None)
                else:
                    obj['spec'][key] = value
            return copy.deepcopy(obj)
        if method == 'DELETE':
            if any(obj['metadata'].get(k) != v for k, v in body.get('preconditions', {}).items()):
                self.error(409)
            del self.objects[path]
            return {}
        if method == 'PUT':
            self.objects[path] = copy.deepcopy(body)
            return copy.deepcopy(body)
        raise AssertionError((method, path))


def ssh_key():
    raw = struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32) + b'x' * 32
    return 'ssh-ed25519 ' + base64.b64encode(raw).decode()


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.portal = module('services/tenant-portal/tenant_portal.py')
        self.portal.log = lambda *a, **k: None
        self.kube = FakeKube()
        self.portal.api = self.kube
        self.ns = 'tenant-direct'
        self.root = '/api/v1/namespaces/' + self.ns
        self.body = {'name': 'demo', 'vcpu': 2, 'memGi': 4, 'gpu': 0,
                     'volume': {'sizeGi': 20}, 'sshPublicKey': ssh_key()}

    def test_devmachine_retry_resumes_without_duplicate_resources(self):
        self.kube.fail = self.root + '/services'
        with self.assertRaises(self.portal.ApiError):
            self.portal.create_devmachine(self.ns, self.body)
        pod = self.kube.objects[self.root + '/pods/demo']
        self.assertTrue(pod['spec']['schedulingGates'])
        uid = pod['metadata']['uid']
        self.portal.create_devmachine(self.ns, self.body)
        self.assertNotIn('schedulingGates', pod['spec'])
        self.assertEqual(len(self.kube.objects), 4)
        self.portal.create_devmachine(self.ns, self.body)
        self.assertEqual(len(self.kube.objects), 4)
        self.assertEqual(pod['metadata']['uid'], uid)
        cm = self.kube.objects[self.root + '/configmaps/demo-ssh']
        self.assertEqual(cm['metadata']['ownerReferences'][0]['uid'], uid)

    def test_response_lost_after_commit_can_be_retried(self):
        self.kube.lose_response = self.root + '/pods'
        with self.assertRaises(urllib.error.URLError):
            self.portal.create_devmachine(self.ns, self.body)
        self.portal.create_devmachine(self.ns, self.body)
        self.assertEqual(len(self.kube.objects), 4)

    def test_changed_request_conflicts_and_never_overwrites(self):
        self.portal.create_devmachine(self.ns, self.body)
        with self.assertRaises(self.portal.ApiError) as ctx:
            self.portal.create_devmachine(self.ns, {**self.body, 'vcpu': 3})
        self.assertEqual(ctx.exception.code, 409)
        self.assertEqual(len(self.kube.objects), 4)

    def test_service_failure_does_not_launch_replicas_then_retry_activates(self):
        self.kube.fail = self.root + '/services'
        body = {'name': 'infer', 'replicas': 2, 'script': 'print("my application")'}
        path = f'/apis/apps/v1/namespaces/{self.ns}/deployments/infer'
        with self.assertRaises(self.portal.ApiError):
            self.portal.create_service(self.ns, body)
        self.assertEqual(self.kube.objects[path]['spec']['replicas'], 0)
        self.portal.create_service(self.ns, body)
        self.assertEqual(self.kube.objects[path]['spec']['replicas'], 2)
        self.assertEqual(len(self.kube.objects), 2)
        self.assertIn('readinessProbe', self.kube.objects[path]['spec']['template']['spec']['containers'][0])

    def test_service_can_share_name_with_existing_development_machine(self):
        self.portal.create_devmachine(self.ns, self.body)
        self.portal.create_service(self.ns, {'name': 'demo', 'replicas': 1})
        self.assertIn(self.root + '/pods/demo', self.kube.objects)
        self.assertEqual(self.kube.objects[f'/apis/apps/v1/namespaces/{self.ns}/deployments/demo']['spec']['replicas'], 1)

    def test_unrelated_volume_is_never_adopted(self):
        self.portal.create_volume(self.ns, {'name': 'demo-data', 'sizeGi': 20, 'class': 'arise-longterm'})
        with self.assertRaises(self.portal.ApiError) as ctx:
            self.portal.create_devmachine(self.ns, self.body)
        self.assertEqual(ctx.exception.code, 409)
        self.assertIn(self.root + '/persistentvolumeclaims/demo-data', self.kube.objects)
        self.assertFalse(any(m == 'DELETE' for m, _, _ in self.kube.calls))

    def test_delete_is_idempotent_and_preserves_data(self):
        self.portal.create_devmachine(self.ns, self.body)
        first = self.portal.delete_workload(self.ns, 'devmachine', 'demo')
        second = self.portal.delete_workload(self.ns, 'devmachine', 'demo')
        self.assertEqual(first['keptVolume'], 'demo-data')
        self.assertEqual(first, second)
        self.assertEqual(list(self.kube.objects), [self.root + '/persistentvolumeclaims/demo-data'])

    def test_recreate_can_reuse_its_retained_volume(self):
        self.portal.create_devmachine(self.ns, self.body)
        pvc = self.kube.objects[self.root + '/persistentvolumeclaims/demo-data']['metadata']['uid']
        self.portal.delete_workload(self.ns, 'devmachine', 'demo')
        self.portal.create_devmachine(self.ns, self.body)
        self.assertEqual(self.kube.objects[self.root + '/persistentvolumeclaims/demo-data']['metadata']['uid'], pvc)

    def test_ssh_rotation_preserves_owner_and_version(self):
        self.portal.create_devmachine(self.ns, self.body)
        before = copy.deepcopy(self.kube.objects[self.root + '/configmaps/demo-ssh']['metadata'])
        self.portal.rotate_ssh_key(self.ns, 'demo', {'sshPublicKey': ssh_key()})
        self.assertEqual(self.kube.objects[self.root + '/configmaps/demo-ssh']['metadata'], before)

    def test_invalid_resources_rejected_before_any_write(self):
        for values in ({'vcpu': 0}, {'gpu': 1.5}, {'gpu': True}, {'memGi': 'inf'},
                       {'name': '../escape'}, {'volume': False}, {'volume': []},
                       {'volume': 'oops'}, {'volume': {'sizeGi': 11}}):
            with self.subTest(values=values), self.assertRaises(self.portal.ApiError) as ctx:
                self.portal.create_devmachine(self.ns, {**self.body, **values})
            self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(self.kube.objects, {})
        for replicas in (0, -1, 1.2, True, 'NaN'):
            with self.subTest(replicas=replicas), self.assertRaises(self.portal.ApiError):
                self.portal.create_job(self.ns, {'name': 'job', 'replicas': replicas})


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / 'ledger.jsonl'
        self.ledger.touch()
        self.book = Path(self.tmp.name) / 'prices.yaml'
        self.book.write_text((ROOT / 'billing/pricebook.yaml').read_text())
        self.mt = module('services/metering/metering.py')
        self.mt.log = lambda *a, **k: None
        self.invoice = module('billing/invoice.py')

    def tearDown(self):
        self.tmp.cleanup()

    def interval(self, uid='u1', start='2026-09-01T00:00:00Z', end='2026-09-01T03:00:00Z', **extra):
        ledger = self.mt.Ledger(str(self.ledger))
        base = dict(pod_uid=uid, tenant='tenant-direct', pod='pod-' + uid, node='n', gpu=1, kind='devmachine', **extra)
        ledger.append(dict(base, event='open', at=start))
        ledger.append(dict(base, event='close', at=end, opened_at=start))

    def run_invoice(self, *extra):
        out = io.StringIO()
        argv = ['invoice.py', '--ledger', str(self.ledger), '--pricebook', str(self.book),
                '--tenants', str(ROOT / 'platform/tenants.yaml'), '--tenant', 'tenant-direct',
                '--from', '2026-09-01T00:00:00Z', '--to', '2026-09-02T00:00:00Z', *extra]
        with patch('sys.argv', argv), redirect_stdout(out):
            status = self.invoice.main()
        return status, list(csv.DictReader(io.StringIO(out.getvalue())))

    def test_partial_dedicated_overlap_is_not_double_billed(self):
        self.interval()
        rc, rows = self.run_invoice('--dedicated-nodes', 'n', '--dedicated-from', '2026-09-01T01:00:00Z', '--dedicated-to', '2026-09-01T02:00:00Z')
        self.assertEqual(rc, 0)
        gpu = [r for r in rows if r['sku'].startswith('gpu-hour')]
        self.assertEqual([r['amount_usd'] for r in gpu], ['9.57', '0.00', '9.57'])

    def test_total_equals_displayed_lines(self):
        for i in range(3):
            self.interval(uid=str(i), end='2026-09-01T00:01:01Z')
        rc, rows = self.run_invoice()
        self.assertEqual(rc, 0)
        self.assertEqual(rows[-1]['amount_usd'], '0.96')

    def test_dedicated_rates_split_at_intraday_change(self):
        with self.book.open('a') as f:
            f.write('\n  - sku: node-month.dgx-b300.dedicated\n    unit: node-month\n    unit_price_micros: 105500000000\n    effective_from: "2026-09-01T12:00:00Z"\n    tenant_kinds: [customer]\n')
        rc, rows = self.run_invoice('--dedicated-nodes', 'n')
        self.assertEqual(rc, 0)
        self.assertEqual([r['amount_usd'] for r in rows[:-1]], ['879.17', '1758.33'])
        self.assertEqual(rows[-1]['amount_usd'], '2637.50')

    def test_missing_ledger_is_not_a_zero_invoice(self):
        self.ledger.unlink()
        rc, rows = self.run_invoice()
        self.assertEqual(rc, 3)
        self.assertEqual(rows, [])

    def test_invalid_pricebook_rejected(self):
        self.book.write_text(self.book.read_text().replace('bill_granularity_seconds: 60', 'bill_granularity_seconds: 0'))
        with self.assertRaises(SystemExit):
            self.run_invoice()


class StateSafetyTests(unittest.TestCase):
    def test_dedicated_binding_is_atomic_and_cleared_on_release(self):
        cc = module('services/capacity-controller/capacity_controller.py')
        changes = []
        cc.patch_node = lambda node, patch: changes.append((node, patch))
        cc.set_direct_binding('worker', 'tenant-blue')
        self.assertEqual(changes[0][1]['metadata']['labels'],
                         {'arise.ai/owner': 'DIRECT', 'arise.ai/tenant': 'tenant-blue'})
        cc.set_owner_label('worker', 'ARISE')
        self.assertIsNone(changes[-1][1]['metadata']['labels']['arise.ai/tenant'])

    def test_meter_readiness_expires_when_polling_stalls(self):
        mt = module('services/metering/metering.py')
        mt.log = lambda *a, **k: None
        with tempfile.TemporaryDirectory() as tmp:
            mt.SEEN_PATH = tmp + '/seen.json'
            mt.list_tenant_pods = lambda: []
            mt.list_tenant_pvcs = lambda: []
            meter = mt.Meter(mt.Ledger(tmp + '/ledger.jsonl'))
            self.assertFalse(meter.is_ready())
            meter.tick()
            self.assertTrue(meter.is_ready())
            self.assertIn('arise_metering_last_success_timestamp_seconds', mt.render_metrics(meter))
            meter.last_success = time.time() - 600
            self.assertFalse(meter.is_ready())

    def test_backup_function_retains_valid_restorable_snapshot(self):
        gw = module('services/gateway/gateway.py')
        backup = module('services/auth-backup/auth_backup.py')
        with tempfile.TemporaryDirectory() as tmp:
            gw.init_auth_store(tmp + '/auth.sqlite3')
            gw.SESSION_KEY = b'x' * 32
            try:
                gw.add_user('admin', 'a-backup-password', 'admin', None, 'Admin')
                first = backup.backup(tmp + '/auth.sqlite3', tmp + '/backups', keep=1)
                self.assertEqual(backup.verify(first['file']), 1)
                gw.add_user('second', 'another-password', 'user', 'tenant-direct', 'Customer')
                second = backup.backup(tmp + '/auth.sqlite3', tmp + '/backups', keep=1)
                self.assertEqual(backup.verify(second['file']), 2)
                self.assertEqual(len(list(Path(tmp + '/backups').glob('auth-*.sqlite3'))), 1)
                restored = module('services/gateway/gateway.py')
                restored.init_auth_store(second['file'])
                try:
                    self.assertTrue(restored.check_login('second', 'another-password'))
                finally:
                    close_store(restored)
            finally:
                close_store(gw)


class EvidenceTests(unittest.TestCase):
    def test_seal_rejects_new_files_and_corrupted_hash_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'scripts').mkdir()
            script = root/'scripts/hash-evidence.sh'
            script.write_text((ROOT/'scripts/hash-evidence.sh').read_text())
            ev = root/'evidence/RUN-lab-test'
            ev.mkdir(parents=True)
            (ev/'result.txt').write_text('PASS')
            env = dict(os.environ, RUN_ID='RUN-lab-test')
            def run(*args):
                return subprocess.run(['bash', str(script), *args], env=env,
                                      capture_output=True, text=True).returncode
            self.assertEqual(run(), 0)
            self.assertEqual(run('verify'), 0)
            (ev/'unsealed.txt').write_text('new evidence')
            self.assertNotEqual(run('verify'), 0)
            (ev/'unsealed.txt').unlink()
            (ev/'hashes.sha256').write_text('not a checksum')
            self.assertNotEqual(run('verify'), 0)


class DeploymentTests(unittest.TestCase):
    def test_public_flags_preserved_before_apply(self):
        dep = module('scripts/render-dgx-platform.py')
        gateway = {'kind': 'Deployment', 'metadata': {'name': 'platform-gateway'},
                   'spec': {'template': {'spec': {'containers': [{'env': [
                       {'name': 'GW_TRUST_PROXY', 'value': 'false'},
                       {'name': 'GW_COOKIE_SECURE', 'value': 'false'}]}]}}}}
        self.assertFalse(dep.public_enabled({}, gateway))
        self.assertTrue(dep.public_enabled({'spec': {'replicas': 1}}, gateway))
        rendered = dep.render([gateway], True)[0]
        self.assertTrue(dep.public_enabled({}, rendered), 'interrupted cutover stays secure')
        env = rendered['spec']['template']['spec']['containers'][0]['env']
        self.assertEqual([e['value'] for e in env], ['true', 'true'])
        self.assertFalse(dep.public_enabled({'spec': {'replicas': 0}}, dep.render([gateway], False)[0]))


if __name__ == '__main__':
    unittest.main(verbosity=2)
