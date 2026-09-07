"""Onboard a previously unknown customer without editing any service source."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parents[1]

def load(rel):
    spec = importlib.util.spec_from_file_location(rel.replace('/', '_'), ROOT/rel)
    obj = importlib.util.module_from_spec(spec); spec.loader.exec_module(obj)
    return obj

class OnboardingTests(unittest.TestCase):
    def test_new_customer_passes_gate_and_can_get_a_persistent_account(self):
        tenant = {'namespace': 'tenant-new-customer', 'short': 'new-customer',
                  'display': 'New customer', 'kind': 'customer', 'owner': 'DIRECT',
                  'queue': 'direct-customer', 'priorities': ['arise-contract-bound']}
        with tempfile.TemporaryDirectory(prefix='alphaapi-onboarding-') as tmp:
            root = Path(tmp)
            for name in ('scripts', 'services', 'platform', 'controller'):
                shutil.copytree(ROOT/name, root/name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            regpath = root/'platform/tenants.yaml'
            reg = yaml.safe_load(regpath.read_text()); reg['spec']['tenants'].append(tenant)
            regpath.write_text(yaml.safe_dump(reg))
            generated = load('scripts/onboard-tenant.py').objects(tenant, 'customer', 'arise-b300', 'dgx')
            (root/'platform/base/new-customer.yaml').write_text(yaml.safe_dump_all(generated))
            kpath = root/'platform/base/kustomization.yaml'
            k = yaml.safe_load(kpath.read_text()); k['resources'].append('new-customer.yaml')
            kpath.write_text(yaml.safe_dump(k))
            result = subprocess.run(['python3', 'scripts/tenant-check.py'], cwd=root,
                                    capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            runtime = root/'tenants.json'
            runtime.write_text(json.dumps({tenant['namespace']: tenant}))
            with patch.dict(os.environ, {'GW_PUBLIC_MODE': 'false', 'GW_COOKIE_SECURE': 'false'}):
                gw = load('services/gateway/gateway.py')
            portal = load('services/tenant-portal/tenant_portal.py')
            gw.TENANTS_PATH = portal.TENANTS_PATH = str(runtime)
            gw.log = portal.log = lambda *a, **k: None
            gw.load_tenants(); portal._load_tenants()
            self.assertEqual(gw.VALID_TENANTS, [tenant['namespace']])
            self.assertEqual(portal.TENANTS[tenant['namespace']]['queue'], tenant['queue'])
            self.assertEqual(portal.PRIORITIES[tenant['namespace']], tenant['priorities'])
            gw.init_auth_store(str(root/'auth.sqlite3'))
            try:
                gw.add_user('new-customer', 'new-customer-password', 'user', tenant['namespace'],
                            'New customer', create_only=True)
                self.assertTrue(gw.check_login('new-customer', 'new-customer-password'))
                self.assertEqual(gw.USERS['new-customer']['tenant'], tenant['namespace'])
            finally:
                gw.AUTH_DB.close(); gw.AUTH_DB_LOCK.close()

class ReleaseReportTests(unittest.TestCase):
    def test_missing_backups_and_access_are_in_final_failure_report(self):
        source = (ROOT/'scripts/verify-dgx.sh').read_text()
        definitions = source[source.index('PASS=0;'):source.index('echo "=== DGX completion gate')]
        tail = source[min(source.index('# --- evidence ---'), source.index('# --- the money record')):]
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp)/'report.json'
            # Exercise the real report and final gates with an absent cluster.
            # Earlier the JSON was written before DGX-38 and falsely said PASS.
            shell = ('set -uo pipefail\n' + definitions + '\n' +
                     'kube() { return 1; }\nK=kube\nCTX=acceptance\nLAUNCH=1\n' +
                     'GPU_NODES=4\nGPU_PER_NODE=8\nFLEET_NODES_PINNED=4\nFLEET_GPUS_PINNED=8\nFLEET_OVERRIDDEN=no\n' +
                     'REPO=' + shlex.quote(str(ROOT)) + '\nOUT=' + shlex.quote(str(report)) + '\n' + tail)
            result = subprocess.run(['bash', '-c', shell], text=True, capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            data = json.loads(report.read_text())
            self.assertEqual(data['gate'], 'FAIL')
            self.assertEqual(data['failed'], 4)
            self.assertEqual({c['id'] for c in data['checks']}, {'DGX-38', 'DGX-39'})

if __name__ == '__main__':
    unittest.main(verbosity=2)
