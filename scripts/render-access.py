#!/usr/bin/env python3
"""Render the optional tenant SSH/service gateway; no cluster mutations."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import yaml

ROOT = Path(__file__).resolve().parents[1]


def manifests(overlay, configuration, tenants, ready=False):
    spec = importlib.util.spec_from_file_location('access_keys', ROOT/'scripts/render-access-keys.py')
    keys = importlib.util.module_from_spec(spec); spec.loader.exec_module(keys)
    known = {t['namespace'] for t in tenants}
    authorized = keys.render(configuration['keys'], known)
    versions = dict(line.split('#',1)[0].strip().split('=',1) for line in (ROOT/'versions.env').read_text().splitlines() if '=' in line.split('#',1)[0])
    image = versions['DEVBOX_IMAGE'] if overlay == 'lab' else configuration.get('image', '')
    if ready:
        if overlay == 'dgx' and authorized:
            if 'day0-registry.invalid' in image or not re.fullmatch(r'[^\s]+@sha256:[0-9a-f]{64}', image):
                raise ValueError('production bastion requires a pushed devbox image pinned by digest')
            hostname = configuration.get('hostname', '')
            if (not isinstance(hostname, str) or len(hostname) > 253 or '.' not in hostname
                    or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in hostname.split('.'))
                    or not re.fullmatch(r'[a-z]{2,63}', hostname.rsplit('.', 1)[-1])):
                raise ValueError('configure a real public SSH hostname before deployment')
    code = {name: (ROOT/'services/ssh-bastion'/name).read_text()
            for name in ('router.py', 'entrypoint.py', 'sshd_config')}
    docs = list(yaml.safe_load_all((ROOT/'platform/access/bastion.yaml').read_text()))
    deployment = next(d for d in docs if d['kind'] == 'Deployment')
    # Removing the last key disables the listener and closes every old stream.
    deployment['spec']['replicas'] = 1 if authorized else 0
    pod = deployment['spec']['template']
    pod['metadata']['annotations'] = {'arise.ai/access-config-sha256': hashlib.sha256(
        json.dumps([code, authorized], sort_keys=True).encode()).hexdigest()}
    pod['spec']['containers'][0]['image'] = image
    if overlay == 'dgx':
        pod['spec']['containers'][0]['ports'][0]['hostPort'] = 2222
    else:
        pod['spec']['containers'][0]['imagePullPolicy'] = 'Never'
    def cm(name, data):
        return {'apiVersion': 'v1', 'kind': 'ConfigMap',
                'metadata': {'name': name, 'namespace': 'access-system'}, 'data': data}
    docs += [cm('tenant-bastion-code', code), cm('tenant-bastion-access-keys', {'authorized_keys': authorized})]
    access_peer = {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'access-system'}},
                   'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'tenant-bastion'}}}
    egress = [{'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                      'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
               'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]}]
    active = sorted({key['tenant'] for key in configuration['keys']})
    for tenant in active:
        for name, selector, port in (
                ('ssh', {'matchLabels': {'arise.ai/kind': 'devmachine'}}, 2222),
                ('service', {'matchExpressions': [{'key': 'arise.ai/service', 'operator': 'Exists'}]}, 8080)):
            docs.append({'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
                'metadata': {'name': 'allow-bastion-' + name, 'namespace': tenant},
                'spec': {'podSelector': selector, 'policyTypes': ['Ingress'],
                         'ingress': [{'from': [access_peer], 'ports': [{'protocol': 'TCP', 'port': port}]}]}})
            egress.append({'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': tenant}},
                                   'podSelector': selector}], 'ports': [{'protocol': 'TCP', 'port': port}]})
    docs.append({'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
        'metadata': {'name': 'bastion-isolation', 'namespace': 'access-system'},
        'spec': {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'],
                 'ingress': [{'ports': [{'protocol': 'TCP', 'port': 2222}]}], 'egress': egress}})
    # Apply the network fences and key material before creating a listener.
    order = {'Namespace': 0, 'ConfigMap': 1, 'PersistentVolumeClaim': 2, 'NetworkPolicy': 3,
             'Service': 4, 'Deployment': 5}
    for doc in docs:
        doc['metadata'].setdefault('labels', {}).update({'project': 'arise-b300', 'arise.ai/component': 'tenant-access'})
    return sorted(docs, key=lambda d: order[d['kind']])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--overlay', choices=('lab', 'dgx'), default='dgx')
    ap.add_argument('--keys', default=str(ROOT/'platform/access/keys.yaml'))
    ap.add_argument('--tenants', default=str(ROOT/'platform/tenants.yaml'))
    ap.add_argument('--ready', action='store_true', help='validate production settings before enabling a non-empty key register')
    args = ap.parse_args()
    try:
        configuration = yaml.safe_load(Path(args.keys).read_text())
        tenants = yaml.safe_load(Path(args.tenants).read_text())['spec']['tenants']
        yaml.safe_dump_all(manifests(args.overlay, configuration, tenants, args.ready), sys.stdout, sort_keys=False)
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        print('Access render refused: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
