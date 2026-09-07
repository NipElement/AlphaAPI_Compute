#!/usr/bin/env python3
"""Verify the deployed tenant bastion against the configured register.

Reads the trusted host public key through the operator's Kubernetes context;
checks the externally presented SSH host key against it when --public is used.
Never prints private keys or changes a cluster resource.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import yaml

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--context', required=True)
    ap.add_argument('--overlay', choices=('lab', 'dgx'), default='dgx')
    ap.add_argument('--keys', default=str(ROOT/'platform/access/keys.yaml'))
    ap.add_argument('--public', action='store_true')
    args = ap.parse_args()
    k = ['kubectl', '--context', args.context]
    def run(command):
        r = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if r.returncode:
            raise ValueError('cluster or public SSH check failed: ' + r.stderr[-250:])
        return r.stdout.strip()
    def get(kind, name, namespace):
        return json.loads(run(k + ['-n', namespace, 'get', kind, name, '-o', 'json']))
    try:
        spec = importlib.util.spec_from_file_location('render_access', ROOT/'scripts/render-access.py')
        renderer = importlib.util.module_from_spec(spec); spec.loader.exec_module(renderer)
        config = yaml.safe_load(Path(args.keys).read_text())
        tenants = yaml.safe_load((ROOT/'platform/tenants.yaml').read_text())['spec']['tenants']
        expected = renderer.manifests(args.overlay, config, tenants, ready=True)
        for item in expected:
            kind, md = item['kind'], item['metadata']
            if kind not in ('Deployment', 'ConfigMap', 'NetworkPolicy', 'PersistentVolumeClaim', 'Service'):
                continue
            live = get(kind, md['name'], md['namespace'])
            if kind == 'ConfigMap':
                if live.get('data') != item.get('data'):
                    raise ValueError('deployed access code/keys differ from configured register')
            elif kind == 'NetworkPolicy':
                if live.get('spec') != item.get('spec'):
                    raise ValueError('access NetworkPolicy differs: ' + md['namespace'] + '/' + md['name'])
            elif kind == 'PersistentVolumeClaim':
                if live['status'].get('phase') != 'Bound' or live['spec']['storageClassName'] != 'arise-longterm':
                    raise ValueError('bastion retained host-key volume is not Bound')
            elif kind == 'Service':
                if live['spec']['selector'] != item['spec']['selector'] or live['spec']['ports'][0]['port'] != 2222:
                    raise ValueError('bastion Service routing differs')
            else:
                if live['spec'].get('replicas') != 1 or live.get('status', {}).get('availableReplicas') != 1:
                    raise ValueError('bastion must have one available replica')
                pod = live['spec']['template']['spec']
                desired = item['spec']['template']['spec']
                if pod.get('automountServiceAccountToken', True) or pod.get('hostNetwork', False):
                    raise ValueError('bastion must have no Kubernetes credential or hostNetwork')
                for field in ('image', 'command', 'securityContext', 'volumeMounts'):
                    if pod['containers'][0][field] != desired['containers'][0][field]:
                        raise ValueError('bastion container configuration differs: ' + field)
                if args.overlay == 'dgx' and pod['containers'][0]['ports'][0].get('hostPort') != 2222:
                    raise ValueError('production bastion hostPort must be 2222')
                want_hash = item['spec']['template']['metadata']['annotations']['arise.ai/access-config-sha256']
                pods = json.loads(run(k + ['-n', 'access-system', 'get', 'pods', '-l',
                                           'app.kubernetes.io/name=tenant-bastion', '-o', 'json']))['items']
                if len(pods) != 1 or pods[0]['metadata'].get('annotations', {}).get('arise.ai/access-config-sha256') != want_hash:
                    raise ValueError('bastion Pod has not rolled out the configured keys/code')
                # ConfigMaps can change without a Pod restart. Compare what the daemon
                # actually sees, in addition to the objects and rollout annotation.
                for path, text in (
                    ('/etc/arise/access-keys/authorized_keys', next(d for d in expected if d['metadata']['name'] == 'tenant-bastion-access-keys')['data']['authorized_keys']),
                    ('/etc/arise/access/router.py', (ROOT/'services/ssh-bastion/router.py').read_text())):
                    mounted = run(k + ['-n', 'access-system', 'exec', 'deploy/tenant-bastion', '--', 'cat', path])
                    if mounted != text.strip():
                        raise ValueError('running bastion has stale mounted code or keys')
        public = run(k + ['-n', 'access-system', 'exec', 'deploy/tenant-bastion', '--',
                         'ssh-keygen', '-y', '-f', '/keys/ssh_host_ed25519_key'])
        if args.public:
            scanned = run(['ssh-keyscan', '-T', '5', '-p', '2222', '-t', 'ed25519', config['hostname']])
            observed = {' '.join(line.split()[1:3]) for line in scanned.splitlines() if not line.startswith('#')}
            if observed != {' '.join(public.split()[:2])}:
                raise ValueError('public SSH host key does not match trusted cluster host key')
        print('PASS: configured keys, running bastion, tenant fences, retained host identity' +
              (' and public SSH endpoint match' if args.public else ' match'))
        return 0
    except (KeyError, TypeError, AttributeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print('Access verification failed: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
