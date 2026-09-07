#!/usr/bin/env python3
"""Render once with secure gateway flags preserved BEFORE applying anything."""
import argparse
import json
import subprocess
import sys
import yaml


def public_enabled(edge, gateway):
    active = edge.get('spec', {}).get('replicas', 0) > 0
    containers = gateway.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])
    env = {e['name']: e.get('value') for c in containers for e in c.get('env', [])}
    # Preserve secure state after an interrupted cutover. Only edge-off resets it.
    return active or any(env.get(k) == 'true' for k in ('GW_TRUST_PROXY', 'GW_COOKIE_SECURE'))


def render(docs, enabled):
    for doc in docs:
        if doc and doc['kind'] == 'Deployment' and doc['metadata']['name'] == 'platform-gateway':
            for c in doc['spec']['template']['spec']['containers']:
                for e in c.get('env', []):
                    if e['name'] in ('GW_COOKIE_SECURE', 'GW_TRUST_PROXY'):
                        e['value'] = str(enabled).lower()
    return docs


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--context', required=True)
    args = ap.parse_args()
    k = ['kubectl', '--context', args.context]
    def get(ns, name):
        # --ignore-not-found distinguishes a first install from an API outage.
        raw = subprocess.check_output(k + ['-n', ns, 'get', 'deployment', name,
                                         '--ignore-not-found', '-o', 'json'], text=True)
        return json.loads(raw) if raw.strip() else {}
    enabled = public_enabled(get('edge-system', 'platform-edge'), get('platform-system', 'platform-gateway'))
    raw = subprocess.check_output(k + ['kustomize', 'platform/overlays/dgx'], text=True)
    yaml.safe_dump_all(render(list(yaml.safe_load_all(raw)), enabled), sys.stdout, sort_keys=False)


if __name__ == '__main__':
    main()
