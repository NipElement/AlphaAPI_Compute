#!/usr/bin/env python3
"""Validate public DNS/ACME settings before exposing a listener."""
from pathlib import Path
import re
import sys
import yaml

docs = list(yaml.safe_load_all(Path('platform/overlays/dgx/edge/caddy.yaml').read_text()))
settings = next(d['data'] for d in docs if d['kind'] == 'ConfigMap')
host, email = settings['CONSOLE_FQDN'], settings['ACME_EMAIL']
if (len(host) > 253 or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', host)
        or host.endswith(('.invalid', '.example', '.test', '.localhost'))
        or not re.fullmatch(r'[^\s@{}$]+@[^\s@{}$]+\.[^\s@{}$]+', email)
        or 'REPLACE_WITH' in email):
    sys.exit('Fill edge/caddy.yaml with a public console FQDN and ACME email before cutover')
print('Edge settings valid: ' + host)
