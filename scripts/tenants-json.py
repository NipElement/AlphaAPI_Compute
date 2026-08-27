#!/usr/bin/env python3
"""Render platform/tenants.yaml as the compact JSON the services consume.

The register is YAML because humans edit it; the services are stdlib-only and
read JSON. `make code` / `make dgx-code` turn this into the platform-tenants
ConfigMap, mounted at /etc/arise/tenants.json — so onboarding a tenant is an
edit HERE plus a regenerate, not a code change in three services.
"""
import json
import sys
from pathlib import Path

import yaml

reg = yaml.safe_load((Path(__file__).resolve().parents[1]
                      / "platform/tenants.yaml").read_text())
out = {t["namespace"]: {"queue": t["queue"], "owner": t["owner"],
                        "priorities": t["priorities"],
                        "display": t.get("display", t["namespace"]),
                        "kind": t.get("kind", "customer")}
       for t in reg["spec"]["tenants"]}
json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
sys.stdout.write("\n")
