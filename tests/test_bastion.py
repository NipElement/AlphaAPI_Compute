#!/usr/bin/env python3
"""No Kubernetes or Docker: forced-command schema, quotas, binary relay."""
import base64
import importlib.util
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]

def load(rel):
    spec = importlib.util.spec_from_file_location('tested_' + rel.replace('/', '_'), ROOT/rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

router = load('services/ssh-bastion/router.py')
keys = load('scripts/render-access-keys.py')


def public():
    raw = struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32) + os.urandom(32)
    return 'ssh-ed25519 ' + base64.b64encode(raw).decode()


class AccessTests(unittest.TestCase):
    def test_target_can_only_address_its_fixed_namespace_and_port(self):
        self.assertEqual(router.destination('tenant-one', 'box'), ('box-ssh.tenant-one.svc.cluster.local', 22))
        self.assertEqual(router.destination('tenant-one', 'service:model'), ('model.tenant-one.svc.cluster.local', 80))
        for name in ('', 'box.other', '../box', 'box:2222', 'service:http://host', '-oProxyCommand=id',
                     'box;id', 'box\n', 'box other', '$(id)', 'service:'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                router.destination('tenant-one', name)

    def test_key_options_are_generated_and_cross_tenant_key_reuse_is_rejected(self):
        key = public()
        out = keys.render([{'tenant': 'tenant-one', 'publicKey': key}], {'tenant-one'})
        self.assertIn('restrict,command=', out)
        self.assertIn('router.py tenant-one"', out)
        with self.assertRaises(ValueError):
            keys.render([{'tenant': 'tenant-one', 'publicKey': key},
                         {'tenant': 'tenant-two', 'publicKey': key}], {'tenant-one', 'tenant-two'})
        for bad in ('command="id" ' + key, key + '\n' + public(), 'ssh-ed25519 AAAA'):
            with self.assertRaises(ValueError):
                keys.render([{'tenant': 'tenant-one', 'publicKey': bad}], {'tenant-one'})
        with self.assertRaises(ValueError):
            keys.render([{'tenant': 'tenant-other', 'publicKey': key}], {'tenant-one'})

    def test_connection_slots_are_bounded_and_recover_after_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            held = [router.acquire_slot(Path(tmp)) for _ in range(router.MAX_TUNNELS)]
            try:
                with self.assertRaises(RuntimeError):
                    router.acquire_slot(Path(tmp))
                os.close(held.pop())
                held.append(router.acquire_slot(Path(tmp)))
            finally:
                for fd in held:
                    os.close(fd)

    def test_manifests_limit_both_directions_and_preserve_host_identity(self):
        renderer = load('scripts/render-access.py')
        configuration = {'keys': [{'tenant': 'tenant-one', 'publicKey': public()}],
                         'hostname': 'ssh.example.com', 'image': 'registry.example.com/devbox@sha256:' + 'a' * 64}
        docs = renderer.manifests('dgx', configuration, [{'namespace': 'tenant-one'}], ready=True)
        deployment = next(d for d in docs if d['kind'] == 'Deployment')
        pod = deployment['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertFalse(pod.get('hostNetwork', False))
        self.assertEqual(pod['containers'][0]['ports'][0]['hostPort'], 2222)
        self.assertTrue(pod['containers'][0]['securityContext']['readOnlyRootFilesystem'])
        self.assertEqual(next(d for d in docs if d['kind'] == 'PersistentVolumeClaim')['spec']['storageClassName'], 'arise-longterm')
        policy = next(d for d in docs if d['metadata']['name'] == 'bastion-isolation')
        for rule in policy['spec']['egress'][1:]:
            for peer in rule['to']:
                self.assertEqual(peer['namespaceSelector']['matchLabels']['kubernetes.io/metadata.name'], 'tenant-one')
                self.assertTrue(peer['podSelector'])
        for d in docs:
            if d['kind'] == 'NetworkPolicy' and d['metadata'].get('namespace') == 'tenant-one':
                peer = d['spec']['ingress'][0]['from'][0]
                self.assertEqual(peer['namespaceSelector']['matchLabels']['kubernetes.io/metadata.name'], 'access-system')
                self.assertEqual(peer['podSelector']['matchLabels']['app.kubernetes.io/name'], 'tenant-bastion')
        configuration['image'] = 'registry.example.com/devbox:latest'
        with self.assertRaises(ValueError):
            renderer.manifests('dgx', configuration, [{'namespace': 'tenant-one'}], ready=True)

    def test_removing_last_key_disables_listener(self):
        renderer = load('scripts/render-access.py')
        docs = renderer.manifests('lab', {'keys': []}, [{'namespace': 'tenant-one'}], ready=True)
        self.assertEqual(next(d for d in docs if d['kind'] == 'Deployment')['spec']['replicas'], 0)
        self.assertFalse(any(d['kind'] == 'NetworkPolicy' and d['metadata'].get('namespace') == 'tenant-one' for d in docs))

    def test_binary_relay_respects_half_close_without_truncation(self):
        local, incoming = socket.socketpair()
        remote, destination = socket.socketpair()
        # relay stdio may use two descriptor numbers referring to the same socket.
        output_fd = os.dup(incoming.fileno())
        payload = os.urandom(2 * 1024 * 1024)
        errors = []
        def run():
            try:
                router.relay(remote, incoming.fileno(), output_fd, idle=5, lifetime=20)
            except Exception as exc:
                errors.append(exc)
            finally:
                try:
                    incoming.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
        tunnel = threading.Thread(target=run)
        tunnel.start()
        def echo():
            while data := destination.recv(65536):
                destination.sendall(data)
            destination.shutdown(socket.SHUT_WR)
        upstream = threading.Thread(target=echo)
        upstream.start()
        def send():
            local.sendall(payload)
            local.shutdown(socket.SHUT_WR)
        sender = threading.Thread(target=send)
        sender.start()
        local.settimeout(15)
        result = bytearray()
        try:
            while chunk := local.recv(65536):
                result.extend(chunk)
            self.assertEqual(result, payload)
            self.assertEqual(errors, [])
        finally:
            for sock in (local, incoming, remote, destination):
                sock.close()
            os.close(output_fd)
            for thread in (sender, upstream, tunnel):
                thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
