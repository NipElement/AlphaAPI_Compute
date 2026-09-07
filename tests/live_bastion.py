#!/usr/bin/env python3
"""Isolated Docker acceptance: pinned host keys, real SSH/scp and service access.

Uses a private bridge without outbound masquerading, a loopback published port and only its own temporary
containers/volumes. Does not modify the platform or any running lab workload.
"""
import importlib.util
import json
from pathlib import Path
import re
import select
import shlex
import socket
import subprocess
import tempfile
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'arise/devbox:lab'


def run(args, **kw):
    result = subprocess.run(args, capture_output=True, text=True, timeout=kw.pop('timeout', 60), **kw)
    if result.returncode:
        raise AssertionError(f"{args[:3]} failed: {result.stderr[-1600:]}")
    return result.stdout.strip()


def main():
    prefix = 'alphaapi-access-' + uuid.uuid4().hex[:8]
    network = prefix + '-net'
    bastion = prefix + '-bastion'
    containers, volumes = [], []
    client = None
    try:
        run(['docker', 'network', 'create', '--opt', 'com.docker.network.bridge.enable_ip_masquerade=false', network])
        with tempfile.TemporaryDirectory(prefix='alphaapi-access-') as tmp:
            tmp = Path(tmp); tmp.chmod(0o755)
            keys = []
            for n in range(3):
                key = tmp/f'key-{n}'
                run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)])
                keys.append(key)
            spec = importlib.util.spec_from_file_location('key_renderer', ROOT/'scripts/render-access-keys.py')
            renderer = importlib.util.module_from_spec(spec); spec.loader.exec_module(renderer)
            entries = [{'tenant': 'tenant-one', 'publicKey': keys[0].with_suffix('.pub').read_text().strip()},
                       {'tenant': 'tenant-two', 'publicKey': keys[1].with_suffix('.pub').read_text().strip()}]
            auth_volume, host_volume = prefix + '-auth', prefix + '-host'
            for name in (auth_volume, host_volume):
                run(['docker', 'volume', 'create', name]); volumes.append(name)
            def write_keys(entries):
                run(['docker', 'run', '--rm', '-i', '--network', 'none', '--user', '0',
                     '-v', auth_volume + ':/config', '--entrypoint', 'python3', IMAGE, '-c',
                     "from pathlib import Path; import sys; p=Path('/config/authorized_keys'); p.write_text(sys.stdin.read()); p.chmod(0o644)"],
                    input=renderer.render(entries, {'tenant-one', 'tenant-two'}))
            write_keys(entries)
            run(['docker', 'run', '--rm', '--network', 'none', '--user', '0',
                 '-v', host_volume + ':/keys', '--entrypoint', 'python3', IMAGE, '-c',
                 "import os; os.chown('/keys',65532,65532); os.chmod('/keys',0o700)"])
            known = tmp/'known_hosts'
            common = ['--network', network, '--user', '65532:65532', '--read-only',
                      '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                      '--sysctl', 'net.ipv4.ip_unprivileged_port_start=0',
                      '--memory', '256m', '--cpus', '0.5',
                      '--tmpfs', '/tmp:uid=65532,gid=65532,mode=1777']
            for n, tenant in enumerate(('tenant-one', 'tenant-two')):
                config = tmp/f'auth-{n}'; config.mkdir(mode=0o755)
                (config/'authorized_keys').write_text(keys[n].with_suffix('.pub').read_text())
                name = prefix + '-target-' + str(n)
                containers.append(name)
                run(['docker', 'run', '-d', '--name', name, *common,
                     '--network-alias', f'box-ssh.{tenant}.svc.cluster.local',
                     '--tmpfs', '/keys:uid=65532,gid=65532,mode=0700',
                     '--tmpfs', '/home/dev:uid=65532,gid=65532,mode=0700',
                     '-v', str(config) + ':/etc/arise/ssh:ro', '--entrypoint', 'sh', IMAGE, '-c',
                     'ssh-keygen -q -t ed25519 -N \"\" -f /keys/ssh_host_ed25519_key && exec /usr/sbin/sshd -D -e -f /etc/ssh/sshd_config -p 22'])
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    try:
                        pub = run(['docker', 'exec', name, 'ssh-keygen', '-y', '-f', '/keys/ssh_host_ed25519_key'])
                        with known.open('a') as f: f.write(f'box.{tenant} {pub}\n')
                        break
                    except AssertionError:
                        time.sleep(.2)
                else: raise AssertionError('target SSH startup timed out')
            containers.append(bastion)
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                published_port = reservation.getsockname()[1]
            run(['docker', 'run', '-d', '--name', bastion, *common,
                 '-p', f'127.0.0.1:{published_port}:2222',
                 '--tmpfs', '/run/access-slots:uid=65532,gid=65532,mode=0700',
                 '-v', str(ROOT/'services/ssh-bastion') + ':/etc/arise/access:ro',
                 '-v', auth_volume + ':/etc/arise/access-keys:ro',
                 '-v', host_volume + ':/keys', '--entrypoint', 'python3', IMAGE,
                 '-u', '/etc/arise/access/entrypoint.py'])
            def ready():
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    result = subprocess.run(['docker', 'exec', bastion, 'test', '-f', '/run/access-slots/ready'],
                                            capture_output=True)
                    if result.returncode == 0: return
                    time.sleep(.3)
                raise AssertionError('bastion readiness timed out')
            ready()
            port = int(run(['docker', 'port', bastion, '2222/tcp']).rsplit(':', 1)[1])
            hostkey = run(['docker', 'exec', bastion, 'ssh-keygen', '-y', '-f', '/keys/ssh_host_ed25519_key'])
            with known.open('a') as f: f.write(f'[127.0.0.1]:{port} {hostkey}\n')
            def outer(n=0):
                return ['ssh', '-T', '-p', str(port), '-i', str(keys[n]),
                        '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                        '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=5',
                        '-o', 'UserKnownHostsFile=' + str(known), 'dev@127.0.0.1']
            def inner(n=0):
                tenant = ('tenant-one', 'tenant-two')[n]
                return ['ssh', '-T', '-i', str(keys[n]), '-o', 'BatchMode=yes',
                        '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
                        '-o', 'UserKnownHostsFile=' + str(known), '-o', 'HostKeyAlias=box.' + tenant,
                        '-o', 'ProxyCommand=' + shlex.join(outer(n) + ['box']), 'dev@box']
            for n in (0, 1):
                assert run(inner(n) + ['id -u']) == '65532'
            print('PASS: both tenants connect to their own machine through a verified SSH bastion', flush=True)
            payload = 'binary-safe-through-two-SSH-hops\n' * 20000
            assert run(inner() + ['cat > /home/dev/roundtrip; cat /home/dev/roundtrip'], input=payload) == payload.strip()
            print('PASS: large data transfer through nested SSH is intact', flush=True)
            for command in ('box.other', 'box;id', '127.0.0.1', 'service:other.tenant-two', ''):
                result = subprocess.run(outer() + [command], capture_output=True, text=True, timeout=10)
                assert result.returncode != 0 and not result.stdout, (command, result.stdout, result.stderr)
            result = subprocess.run(outer(2) + ['box'], capture_output=True, text=True, timeout=10)
            assert result.returncode != 0 and 'Permission denied' in result.stderr
            args = outer(); args[-1:-1] = ['-W', 'box-ssh.tenant-two.svc.cluster.local:22']
            result = subprocess.run(args, capture_output=True, text=True, timeout=10)
            assert result.returncode != 0 and 'administratively prohibited' in result.stderr, result.stderr
            print('PASS: cross-namespace addresses, shell injection, unknown keys and arbitrary TCP forwarding denied', flush=True)
            service = prefix + '-service'; containers.append(service)
            run(['docker', 'run', '-d', '--name', service, *common,
                 '--network-alias', 'model.tenant-one.svc.cluster.local', '--entrypoint', 'python3', IMAGE,
                 '-c', "from http.server import BaseHTTPRequestHandler,HTTPServer\nclass H(BaseHTTPRequestHandler):\n def do_GET(self):\n  self.send_response(200); self.end_headers(); self.wfile.write(b'private-model-one')\nHTTPServer(('0.0.0.0',80),H).serve_forever()"])
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    run(['docker', 'exec', service, 'python3', '-c',
                         "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:80',timeout=1).status == 200"])
                    break
                except AssertionError:
                    time.sleep(.2)
            else:
                raise AssertionError('service fixture did not become ready')
            client = subprocess.Popen(['python3', str(ROOT/'services/ssh-bastion/client.py'), 'service', 'model',
                '--host', '127.0.0.1', '--port', str(port), '--key', str(keys[0]),
                '--known-hosts', str(known), '--local-port', '0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert select.select([client.stdout], [], [], 5)[0]
            line = client.stdout.readline()
            local = int(re.search(r'127\.0\.0\.1:(\d+)', line).group(1))
            with urllib.request.urlopen(f'http://127.0.0.1:{local}/', timeout=15) as response:
                assert response.read() == b'private-model-one'
            client.terminate(); client.wait(timeout=5); client = None
            print('PASS: private HTTP service works through the loopback-only tenant tunnel', flush=True)
            run(['docker', 'restart', bastion]); ready()
            assert run(['docker', 'exec', bastion, 'ssh-keygen', '-y', '-f', '/keys/ssh_host_ed25519_key']) == hostkey
            assert run(inner() + ['id -u']) == '65532'
            write_keys(entries[1:])
            result = subprocess.run(outer() + ['box'], capture_output=True, text=True, timeout=10)
            assert result.returncode != 0 and 'Permission denied' in result.stderr
            assert run(inner(1) + ['id -u']) == '65532'
            logs = run(['docker', 'logs', bastion])
            assert '"event": "connect"' in logs and '"tenant": "tenant-one"' in logs
            print('PASS: host key survives restart, removed access key is denied, other tenant remains usable, audit reaches container logs', flush=True)
    except Exception:
        for name in containers:
            result = subprocess.run(['docker', 'logs', '--tail', '15', name], capture_output=True, text=True)
            print(name, result.stdout, result.stderr)
        raise
    finally:
        if client:
            client.terminate(); client.wait(timeout=5)
        for name in reversed(containers):
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
        for name in volumes:
            subprocess.run(['docker', 'volume', 'rm', name], capture_output=True)
        subprocess.run(['docker', 'network', 'rm', network], capture_output=True)


if __name__ == '__main__':
    main()
