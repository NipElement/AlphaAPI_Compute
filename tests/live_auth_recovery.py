#!/usr/bin/env python3
"""Isolated process acceptance: concurrency, SIGKILL, SQLite backup/restore, limits.

Uses only temporary files and loopback HTTP. Never reads a deployed auth database,
never needs Kubernetes, and never prints passwords, sessions or database records.
Run: python3 tests/live_auth_recovery.py
"""
import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / 'services/gateway/gateway.py'
BACKUP = ROOT / 'services/auth-backup/auth_backup.py'


class ProcessRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='alphaapi-auth-recovery-')
        self.root = Path(self.tmp.name)
        self.db = self.root / 'auth.sqlite3'
        self.processes, self.logs, self.sockets = [], [], []
        self.pw = secrets.token_urlsafe(24)
        self.key = secrets.token_urlsafe(48)
        (self.root / 'index.html').write_text('<!doctype html><title>Recovery fixture</title>')
        (self.root / 'tenants.json').write_text(json.dumps({'tenant-arise': {}, 'tenant-direct': {}}))
        self.port, self.server = self.start(self.db)
        self.admin = self.login('admin')

    def tearDown(self):
        for s in self.sockets:
            s.close()
        for p in self.processes:
            if p.poll() is None:
                p.terminate()
            p.wait(timeout=10)
        for f in self.logs:
            f.close()
        self.tmp.cleanup()

    def start(self, database, key=None, expected=True):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = {**os.environ, 'PORT': str(port), 'WEB_DIR': str(self.root),
               'GW_PUBLIC_MODE': 'true', 'GW_COOKIE_SECURE': 'true',
               'GW_AUTH_DB': str(database), 'GW_SESSION_KEY': key or self.key,
               'GW_ADMIN_PASSWORD': self.pw, 'GW_ARISE_PASSWORD': self.pw,
               'GW_DIRECT_PASSWORD': self.pw, 'TENANTS_PATH': str(self.root / 'tenants.json'),
               'GW_MAX_CONNECTIONS': '16', 'GW_SOCKET_TIMEOUT_SECONDS': '2',
               'GW_TRUST_PROXY': 'false'}
        log = tempfile.TemporaryFile()
        self.logs.append(log)
        p = subprocess.Popen([sys.executable, str(GATEWAY)], env=env, stdout=log, stderr=log)
        self.processes.append(p)
        if not expected:
            self.assertNotEqual(p.wait(timeout=10), 0)
            return port, p
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.assertIsNone(p.poll(), 'gateway unexpectedly exited at startup')
            try:
                if self.request('GET', '/readyz', port=port)[0] == 200:
                    return port, p
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(.05)
        self.fail('gateway did not become ready')

    def request(self, method, path, body=None, cookie=None, port=None):
        c = http.client.HTTPConnection('127.0.0.1', port or self.port, timeout=15)
        try:
            headers = {'Content-Type': 'application/json', 'Connection': 'close'}
            if cookie:
                headers['Cookie'] = cookie
            c.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
            r = c.getresponse()
            data = json.loads(r.read())
            return r.status, data, r.getheader('Set-Cookie', '').split(';')[0]
        finally:
            c.close()

    def login(self, name, port=None):
        status, _, cookie = self.request('POST', '/auth/login', {'username': name, 'password': self.pw}, port=port)
        self.assertEqual(status, 200)
        self.assertTrue(cookie.startswith('__Host-arise_session='))
        return cookie

    def create(self, name):
        return self.request('POST', '/auth/users', {'username': name, 'password': self.pw,
                            'role': 'user', 'tenant': 'tenant-arise'}, self.admin)[0]

    def test_concurrent_duplicate_account_creation_has_exactly_one_winner(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(self.create, ['same-user'] * 8))
        self.assertEqual(sorted(statuses), [201] + [409] * 7)
        self.login('same-user')
        self.assertEqual(self.request('GET', '/auth/users', cookie=self.admin)[0], 200)

    def test_acknowledged_writes_and_revocations_survive_sigkill_during_writes(self):
        self.assertEqual(self.create('durable-user'), 201)
        valid = self.login('durable-user')
        revoked = self.login('durable-user')
        self.assertEqual(self.request('POST', '/auth/logout', {}, revoked)[0], 200)
        self.assertEqual(self.create('deleted-user'), 201)
        self.assertEqual(self.request('DELETE', '/auth/users/deleted-user', cookie=self.admin)[0], 200)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(self.create, 'writer-' + str(i)): 'writer-' + str(i) for i in range(12)}
            done, _ = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED)
            self.assertTrue(any(f.result() == 201 for f in done))
            self.server.kill()
            self.server.wait(timeout=10)
            acknowledged = []
            for f, name in futures.items():
                try:
                    if f.result() == 201:
                        acknowledged.append(name)
                except (OSError, http.client.HTTPException):
                    pass  # in-flight requests have no success acknowledgement
        self.port, self.server = self.start(self.db)
        self.assertEqual(self.request('GET', '/auth/me', cookie=valid)[0], 200)
        self.assertEqual(self.request('GET', '/auth/me', cookie=revoked)[0], 401)
        status, data, _ = self.request('GET', '/auth/users', cookie=self.admin)
        self.assertEqual(status, 200)
        names = {u['name'] for u in data['users']}
        self.assertTrue(set(acknowledged).issubset(names))
        self.assertNotIn('deleted-user', names)
        self.login('durable-user')

    def test_live_backup_restore_in_new_process_and_signing_key_rotation(self):
        self.assertEqual(self.create('restore-user'), 201)
        old_cookie = self.login('restore-user')
        revoked_cookie = self.login('restore-user')
        self.request('POST', '/auth/logout', {}, revoked_cookie)
        # Keep the source gateway serving/writing during the SQLite backup.
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.create, 'backup-writer-' + str(i)) for i in range(8)]
            result = subprocess.run([sys.executable, str(BACKUP), '--source', str(self.db),
                                     '--destination', str(self.root / 'backups')],
                                    capture_output=True, text=True, check=True, timeout=20)
            metadata = json.loads(result.stdout)
            self.assertEqual([f.result() for f in futures], [201] * 8)
        backup = Path(metadata['file'])
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        restored = self.root / 'restored' / 'auth.sqlite3'
        restored.parent.mkdir()
        shutil.copyfile(backup, restored)
        restored_port, restored_process = self.start(restored)
        self.assertEqual(self.request('GET', '/auth/me', cookie=old_cookie, port=restored_port)[0], 200)
        self.assertEqual(self.request('GET', '/auth/me', cookie=revoked_cookie, port=restored_port)[0], 401)
        self.login('restore-user', restored_port)
        restored_process.terminate()
        restored_process.wait(timeout=10)
        rotated_port, _ = self.start(restored, key=secrets.token_urlsafe(48))
        self.assertEqual(self.request('GET', '/auth/me', cookie=old_cookie, port=rotated_port)[0], 401)
        self.login('restore-user', rotated_port)

    def test_corrupt_database_and_competing_writer_refuse_to_start(self):
        self.start(self.db, expected=False)
        corrupt = self.root / 'corrupt.sqlite3'
        corrupt.write_bytes(b'not a database\0' * 1024)
        self.start(corrupt, expected=False)
        self.assertEqual(self.request('GET', '/auth/me', cookie=self.admin)[0], 200)
        self.assertEqual(corrupt.read_bytes(), b'not a database\0' * 1024)

    def test_connection_ceiling_recovers_after_idle_clients_timeout(self):
        # Occupy all 16 allowed workers with incomplete HTTP requests.
        for _ in range(16):
            s = socket.create_connection(('127.0.0.1', self.port), timeout=2)
            s.sendall(b'GET /healthz HTTP/1.1\r\nHost: localhost\r\n')
            self.sockets.append(s)
        time.sleep(.15)
        extra = socket.create_connection(('127.0.0.1', self.port), timeout=2)
        self.sockets.append(extra)
        try:
            extra.sendall(b'GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n')
            self.assertEqual(extra.recv(64), b'')
        except ConnectionResetError:
            pass
        time.sleep(2.2)
        self.assertEqual(self.request('GET', '/readyz')[0], 200)
        self.assertEqual(self.request('GET', '/auth/me', cookie=self.admin)[0], 200)


if __name__ == '__main__':
    unittest.main(verbosity=2)
