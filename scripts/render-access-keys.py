#!/usr/bin/env python3
"""Render tenant-scoped forced SSH commands from registered Ed25519 public keys."""
import argparse
import base64
import datetime
from pathlib import Path
import re
import sys
import yaml


def render(entries, registered):
    if not isinstance(entries, list):
        raise ValueError('keys must be a list')
    lines, seen = [], set()
    for entry in entries:
        tenant, public = entry.get('tenant'), entry.get('publicKey')
        if tenant not in registered or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', tenant):
            raise ValueError('key must belong to a registered tenant')
        if not isinstance(public, str) or len(public) > 1024 or '\n' in public or '\r' in public:
            raise ValueError('one Ed25519 public key per entry is required')
        fields = public.split()
        if len(fields) < 2 or fields[0] != 'ssh-ed25519':
            raise ValueError('bastion access requires an Ed25519 key without authorized_keys options')
        raw = base64.b64decode(fields[1], validate=True)
        if len(raw) != 51 or raw[:19] != b'\0\0\0\x0bssh-ed25519\0\0\0\x20':
            raise ValueError('invalid Ed25519 key encoding')
        if raw in seen:
            raise ValueError('a public key may appear only once; use different keys for different tenants')
        seen.add(raw)
        options = ['restrict', f'command="/usr/local/bin/python3 -I /etc/arise/access/router.py {tenant}"']
        expiry = entry.get('expiresAt')
        if expiry:
            date = datetime.datetime.strptime(expiry, '%Y-%m-%dT%H:%M:%SZ')
            options.append('expiry-time="' + date.strftime('%Y%m%d%H%M%SZ') + '"')
        lines.append(','.join(options) + f' ssh-ed25519 {fields[1]} {tenant}')
    return '\n'.join(lines) + ('\n' if lines else '')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--keys', default='platform/access/keys.yaml')
    ap.add_argument('--tenants', default='platform/tenants.yaml')
    ap.add_argument('--require-keys', action='store_true')
    args = ap.parse_args()
    try:
        tenants = yaml.safe_load(Path(args.tenants).read_text())['spec']['tenants']
        keys = yaml.safe_load(Path(args.keys).read_text())['keys']
        output = render(keys, {t['namespace'] for t in tenants})
        if args.require_keys and not output:
            raise ValueError('configure a registered tenant public key before enabling the bastion')
        sys.stdout.write(output)
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        print('Invalid access register: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
