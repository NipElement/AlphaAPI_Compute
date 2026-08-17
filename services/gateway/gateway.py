#!/usr/bin/env python3
"""
ARISE Platform Console — the single front door (one port, every module).

Shape follows the Volcengine MLP console: login → left-nav workbench, with
role-gated modules (their 权限管理 model): an ADMIN sees the workbench plus
the operator plane (fleet / monitoring / alerts / audit / user management);
a tenant USER sees only the workbench, locked to their own tenant.

Auth (prelab-grade, honestly scoped):
  - Username/password sessions: PBKDF2-HMAC-SHA256, HttpOnly cookie, 12h TTL,
    in-memory session store. Seed users come from env (defaults documented in
    README). Runtime-created users live in memory — the real product replaces
    this whole block with OIDC/SSO; what is permanent is the ENFORCEMENT
    below, which is exactly what an IdP would also front.
  - Enforcement happens at the proxy, not in the SPA: a tenant user's requests
    to /oapi/* or to another tenant's ns are refused with 403 regardless of
    what any UI shows. Hidden nav is UX; the proxy check is the control.

Privilege design — this process holds NO Kubernetes credentials:
  - automountServiceAccountToken: false (UI-03 asserts the empty mount);
  - pure HTTP aggregation over split-RBAC backends:
        /papi/*  -> tenant-portal   (workload RBAC, tenant namespaces only)
        /oapi/*  -> ops-console     (NodeOwnership intent, no node writes)
        /prom/*  -> prometheus      (GET query/query_range only)
        /am/*    -> alertmanager    (GET alerts only)

Dependencies: Python standard library only.
"""

import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The built Vue/Arco SPA (web/dist) is mounted here read-only. The gateway
# serves it same-origin so the session cookie and the JSON/proxy API share an
# origin — no CORS, no token in JS.
WEB_DIR = os.environ.get("WEB_DIR", "/app/web")

PORT = int(os.environ.get("PORT", "8080"))
BACKENDS = {
    "papi": os.environ.get("PORTAL_URL", "http://tenant-portal.platform-system.svc:8080"),
    "oapi": os.environ.get("CONSOLE_URL", "http://ops-console.platform-system.svc:8080"),
    "prom": os.environ.get("PROMETHEUS_URL", "http://prometheus.monitoring.svc:9090"),
    "am":   os.environ.get("ALERTMANAGER_URL", "http://alertmanager.monitoring.svc:9093"),
}
READONLY_PATHS = {
    "prom": ("/api/v1/query", "/api/v1/query_range"),
    "am":   ("/api/v2/alerts",),
}
# Modules a tenant user may reach. Everything else is admin-only at the PROXY.
# /prom is admin-only: it exposes fleet-wide operator metrics (node ownership,
# active contracts, other tenants' PVC labels) with arbitrary PromQL and no
# tenant scoping. No tenant-facing view consumes it — Monitoring is admin-only.
USER_ALLOWED_BACKENDS = {"papi"}

SESSION_TTL = 12 * 3600
COOKIE = "arise_session"
MAX_BODY = 1 << 20  # 1 MiB — control-plane JSON payloads are tiny; caps memory use


def log(level, msg, **kw):
    rec = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "level": level, "component": "gateway", "msg": msg}
    rec.update(kw)
    print(json.dumps(rec), flush=True)


# ------------------------------- user store ---------------------------------
def _pw_hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120000).hex()


USERS: dict = {}
SESSIONS: dict = {}


def add_user(name, password, role, tenant, display):
    salt = secrets.token_bytes(16)
    USERS[name] = {"salt": salt, "hash": _pw_hash(password, salt),
                   "role": role, "tenant": tenant, "display": display}


def seed_users():
    """Seed accounts; passwords via env, defaults documented in README.
    Real product: this whole store is replaced by OIDC — the seeds exist so
    the role-gated product experience is testable end to end today."""
    add_user("admin", os.environ.get("GW_ADMIN_PASSWORD", "arise-admin"),
             "admin", None, "平台管理员")
    add_user("arise-dev", os.environ.get("GW_ARISE_PASSWORD", "arise-dev"),
             "user", "tenant-arise", "内部研发")
    add_user("direct-cust", os.environ.get("GW_DIRECT_PASSWORD", "direct-cust"),
             "user", "tenant-direct", "Direct 客户")


def check_login(name, password):
    u = USERS.get(name)
    if not u:
        # burn comparable time so absent users are not distinguishable
        _pw_hash(password, b"0" * 16)
        return None
    return u if hmac.compare_digest(u["hash"], _pw_hash(password, u["salt"])) else None


def new_session(name):
    tok = secrets.token_urlsafe(32)
    SESSIONS[tok] = {"user": name, "exp": time.time() + SESSION_TTL}
    return tok


def session_user(handler):
    """Resolve the session cookie to a live user record, or None."""
    raw = handler.headers.get("Cookie", "")
    tok = None
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            tok = v
    if not tok:
        return None
    sess = SESSIONS.get(tok)
    if not sess or sess["exp"] < time.time():
        SESSIONS.pop(tok, None)
        return None
    u = USERS.get(sess["user"])
    if not u:
        return None
    return {"name": sess["user"], "role": u["role"],
            "tenant": u["tenant"], "display": u["display"], "_tok": tok}


def public_user(name):
    u = USERS[name]
    return {"name": name, "role": u["role"], "tenant": u["tenant"],
            "display": u["display"]}


# --------------------------------- handler ----------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "arise-gateway/2.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8",
              extra_headers=None, cache="no-store"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        if ctype.startswith("text/html"):
            self.send_header("X-Frame-Options", "DENY")
            # The Arco bundle is external JS (no inline scripts), so script-src
            # needs no 'unsafe-inline'. Vue/Arco inject styles at runtime, so
            # style-src keeps it. data: covers Arco's inline SVG/font assets.
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self'; "
                             "style-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data:; font-src 'self' data:; "
                             "connect-src 'self'")
        for h, v in (extra_headers or []):
            self.send_header(h, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        """Request body, hard-capped at MAX_BODY. Sends 413 and returns None on
        overrun so a huge/lying Content-Length can never allocate unbounded
        memory (unauthenticated on the login path)."""
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            self._send(413, {"error": "request body too large"})
            return None
        return self.rfile.read(n) if n > 0 else b""

    def _body_json(self):
        raw = self._read_body()
        if raw is None:
            return None            # 413 already sent — caller must stop
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    # ------------------------------ auth API --------------------------------
    def _auth(self, method, path):
        if path == "/auth/login" and method == "POST":
            b = self._body_json()
            if b is None:
                return True        # 413 already sent
            u = check_login(str(b.get("username", "")), str(b.get("password", "")))
            if not u:
                log("WARN", "login failed", user=str(b.get("username", ""))[:32])
                self._send(401, {"error": "用户名或密码错误 / bad credentials"})
                return True
            tok = new_session(b["username"])
            log("INFO", "login", user=b["username"], role=u["role"])
            self._send(200, public_user(b["username"]), extra_headers=[(
                "Set-Cookie",
                f"{COOKIE}={tok}; HttpOnly; Path=/; SameSite=Lax; Max-Age={SESSION_TTL}")])
            return True

        me = session_user(self)
        if path == "/auth/me" and method == "GET":
            self._send(200 if me else 401,
                       public_user(me["name"]) if me else {"error": "unauthenticated"})
            return True
        if path == "/auth/logout" and method == "POST":
            if me:
                SESSIONS.pop(me["_tok"], None)
            self._send(200, {"ok": True}, extra_headers=[(
                "Set-Cookie", f"{COOKIE}=; HttpOnly; Path=/; Max-Age=0")])
            return True

        # ---- user management: admin only -----------------------------------
        if path == "/auth/users" or path.startswith("/auth/users/"):
            if not me:
                self._send(401, {"error": "unauthenticated"})
                return True
            if me["role"] != "admin":
                self._send(403, {"error": "user management is admin-only"})
                return True
            if method == "GET":
                self._send(200, {"users": [public_user(n) for n in sorted(USERS)]})
                return True
            if method == "POST" and path == "/auth/users":
                b = self._body_json()
                if b is None:
                    return True    # 413 already sent
                name = str(b.get("username", "")).strip()
                pw = str(b.get("password", ""))
                role = b.get("role", "user")
                tenant = b.get("tenant")
                if not name or not name.replace("-", "").replace("_", "").isalnum():
                    self._send(400, {"error": "invalid username"})
                elif name in USERS:
                    self._send(409, {"error": f"user {name} exists"})
                elif len(pw) < 8:
                    self._send(400, {"error": "password must be at least 8 characters"})
                elif role not in ("admin", "user"):
                    self._send(400, {"error": "role must be admin or user"})
                elif role == "user" and tenant not in ("tenant-arise", "tenant-direct"):
                    self._send(400, {"error": "a user needs a tenant"})
                else:
                    add_user(name, pw, role, tenant if role == "user" else None,
                             str(b.get("display", name))[:48])
                    log("INFO", "user created", user=name, role=role, by=me["name"])
                    self._send(201, public_user(name))
                return True
            if method == "DELETE" and path.startswith("/auth/users/"):
                name = path.rsplit("/", 1)[1]
                if name == me["name"]:
                    self._send(400, {"error": "cannot delete yourself"})
                elif name not in USERS:
                    self._send(404, {"error": "no such user"})
                elif USERS[name]["role"] == "admin" and \
                        sum(1 for u in USERS.values() if u["role"] == "admin") == 1:
                    self._send(400, {"error": "cannot delete the last admin"})
                else:
                    USERS.pop(name)
                    SESSIONS_DROP = [t for t, s2 in SESSIONS.items() if s2["user"] == name]
                    for t in SESSIONS_DROP:
                        SESSIONS.pop(t, None)
                    log("INFO", "user deleted", user=name, by=me["name"])
                    self._send(200, {"deleted": name})
                return True
        return False

    # ------------------------------- proxy ----------------------------------
    def _proxy(self, method):
        me = session_user(self)
        if not me:
            self._send(401, {"error": "unauthenticated"})
            return
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        key = parts[0]
        base = BACKENDS.get(key)
        if not base:
            self._send(404, {"error": "not found"})
            return
        # ---- role enforcement (the control; hidden nav is only UX) ---------
        if me["role"] != "admin":
            if key not in USER_ALLOWED_BACKENDS:
                self._send(403, {"error": f"module '{key}' is admin-only"})
                return
            if key == "papi":
                q = urllib.parse.parse_qs(parsed.query)
                ns = (q.get("ns") or [me["tenant"]])[0]
                if ns != me["tenant"]:
                    log("WARN", "cross-tenant refused", user=me["name"], ns=ns)
                    self._send(403, {"error": f"your account is scoped to {me['tenant']}"})
                    return
        rest = "/" + "/".join(parts[1:])
        if key in READONLY_PATHS:
            if method != "GET" or rest not in READONLY_PATHS[key]:
                self._send(403, {"error": f"{key} proxy is GET-only on an allow-list"})
                return
            url = f"{base}{rest}"
        else:
            url = f"{base}/api{rest}"
        # Authoritatively pin the tenant for non-admin papi. The check above 403s an
        # EXPLICIT cross-tenant ns; this closes the OMITTED-ns hole: never forward a
        # papi request whose ns is absent (the backend's own default must never be
        # reachable). We strip any client ns and re-inject me['tenant'].
        fwd_query = parsed.query
        if me["role"] != "admin" and key == "papi":
            q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            q["ns"] = [me["tenant"]]
            fwd_query = urllib.parse.urlencode(q, doseq=True)
        if fwd_query:
            url += f"?{fwd_query}"
        data = None
        if method in ("POST", "PUT"):
            data = self._read_body()
            if data is None:
                return             # 413 already sent
        req = urllib.request.Request(url, method=method, data=data)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                self._send(resp.status, resp.read(),
                           resp.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as exc:
            self._send(exc.code, exc.read(),
                       exc.headers.get("Content-Type", "application/json"))
        except Exception as exc:                             # noqa: BLE001
            log("ERROR", "upstream call failed", backend=key,
                error_class=type(exc).__name__)
            self._send(502, {"error": "upstream service unavailable"})

    # ---------------------------- static SPA --------------------------------
    def _serve_static(self, path):
        """Serve the built SPA. Auth is handled client-side (the router guard
        calls /auth/me); the API/proxy endpoints below enforce the real control.
        Hash routing means every deep link is '/', so unknown paths fall back to
        index.html rather than 404, and path traversal is rejected."""
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        root = os.path.realpath(WEB_DIR)
        full = os.path.realpath(os.path.join(root, rel))
        if full != root and not full.startswith(root + os.sep):
            self._send(404, {"error": "not found"})
            return
        if not os.path.isfile(full):
            # A missing hashed asset is a real 404 — never fall back to index.html
            # there, or a stale/typo'd asset URL would be cached as HTML.
            if path.startswith("/assets/"):
                self._send(404, {"error": "not found"})
                return
            full = os.path.join(root, "index.html")     # SPA deep-link fallback
            if not os.path.isfile(full):
                self._send(503, {"error": "console assets not mounted"})
                return
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            self._send(404, {"error": "not found"})
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") and "charset" not in ctype:
            ctype += "; charset=utf-8"
        # Content-hashed assets are immutable; index.html must always revalidate.
        cache = ("public, max-age=31536000, immutable"
                 if path.startswith("/assets/") else "no-store")
        self._send(200, body, ctype, cache=cache)

    # ------------------------------- routes ---------------------------------
    def do_GET(self):                                        # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz":                          # liveness: process is up
            self._send(200, {"status": "ok"})
            return
        if path == "/readyz":                           # readiness: can serve the SPA
            ok = os.path.isfile(os.path.join(WEB_DIR, "index.html"))
            self._send(200 if ok else 503,
                       {"status": "ok" if ok else "web assets not mounted"})
            return
        if self._auth("GET", path):
            return
        parts = [p for p in path.split("/") if p]
        if parts and parts[0] in BACKENDS:              # papi/oapi/prom/am
            self._proxy("GET")
            return
        self._serve_static(path)                        # the SPA and its assets

    def do_POST(self):                                       # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if self._auth("POST", path):
            return
        self._proxy("POST")

    def do_DELETE(self):                                     # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if self._auth("DELETE", path):
            return
        self._proxy("DELETE")



def main():
    seed_users()
    log("INFO", "gateway listening", port=PORT, backends=list(BACKENDS),
        users=len(USERS),
        note="session auth + role enforcement at the proxy; no k8s credentials")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
