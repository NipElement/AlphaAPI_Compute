#!/usr/bin/env python3
"""
ARISE Platform Console — the single front door (one port, every module).

Shape follows the Volcengine MLP console: login → left-nav workbench, with
role-gated modules (their 权限管理 model): an ADMIN sees the workbench plus
the operator plane (fleet / monitoring / alerts / audit / user management);
a tenant USER sees only the workbench, locked to their own tenant.

Auth:
  - Username/password login: PBKDF2-HMAC-SHA256 (120k), throttled per source IP
    AND per username with lockout, so credential stuffing is neither cheap nor
    quiet. Seed passwords come from env.
  - Sessions are STATELESS signed tokens (HMAC-SHA256 over user + identity
    version + expiry + token id), in an HttpOnly/SameSite=Lax cookie —
    Secure + __Host- prefixed in public mode. Stateless because the previous
    in-process session dict made every deploy a mass logout of paying
    customers and made a second replica impossible. Role and tenant are
    re-read from the user store on every request, so a demotion or a deletion
    takes effect on the next request rather than at the next login; logout
    records the token id until its own expiry.
  - GW_PUBLIC_MODE=true (set by the dgx overlay) turns prelab conveniences
    into launch requirements and FAILS CLOSED: no seed password from the
    README, none shorter than 12 chars, and a stable GW_SESSION_KEY, or the
    process refuses to start.
  - Enforcement happens at the proxy, not in the SPA: a tenant user's requests
    to /oapi/* or to another tenant's ns are refused with 403 regardless of
    what any UI shows. Hidden nav is UX; the proxy check is the control.
  - Cross-origin writes are refused (Origin vs Host) as the CSRF layer above
    SameSite. The user store itself is still in-process — that is what the
    identity provider (decision D3) replaces; the enforcement above is what
    survives that change.

Privilege design — this process holds NO Kubernetes credentials:
  - automountServiceAccountToken: false (UI-03 asserts the empty mount);
  - pure HTTP aggregation over split-RBAC backends:
        /papi/*  -> tenant-portal   (workload RBAC, tenant namespaces only)
        /oapi/*  -> ops-console     (NodeOwnership intent, no node writes)
        /prom/*  -> prometheus      (GET query/query_range only)
        /am/*    -> alertmanager    (GET alerts only)

Dependencies: Python standard library only.
"""

import base64
import hashlib
import hmac
import json
import math
import mimetypes
import os
import secrets
import socket
import sys
import threading
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
MAX_BODY = 1 << 20  # 1 MiB — control-plane JSON payloads are tiny; caps memory use

# ============================ public-mode posture ============================
# GW_PUBLIC_MODE is the single switch that turns prelab conveniences into
# launch requirements. The dgx overlay sets it true; the lab leaves it false so
# `make deploy` keeps working with no secret plumbing. Everything it gates
# fails CLOSED — a missing secret stops the process rather than silently
# serving a documented default password on the public internet.
PUBLIC_MODE = os.environ.get("GW_PUBLIC_MODE", "false").lower() == "true"
# Secure + __Host- prefix require HTTPS. Default follows PUBLIC_MODE; a
# separate switch exists so a TLS-terminating edge can be staged independently.
COOKIE_SECURE = os.environ.get(
    "GW_COOKIE_SECURE", "true" if PUBLIC_MODE else "false").lower() == "true"
# __Host- is enforced by the BROWSER: it refuses the cookie unless Secure,
# Path=/ and no Domain — so a compromised sibling host cannot plant a session.
COOKIE = "__Host-arise_session" if COOKIE_SECURE else "arise_session"
# X-Forwarded-For is attacker-controlled unless a trusted proxy sets it. Only
# honour it when the deployment says an edge is in front (rate limiting and
# audit logs both key on the result).
TRUST_PROXY = os.environ.get("GW_TRUST_PROXY", "false").lower() == "true"

# Login throttle. Each failed attempt costs ~120k PBKDF2 iterations in a pod
# capped at 300m CPU, so unthrottled credential stuffing is both a break-in
# attempt and a denial of service.
#
# The two keys get DIFFERENT policies on purpose. A lockout keyed on the
# attacker's own IP costs the attacker and nobody else, so it is strict. A
# lockout keyed on a USERNAME is collateral by construction — anyone who knows
# an account name could otherwise keep that customer locked out indefinitely,
# turning our own defence into their denial of service. So the username key is
# deliberately looser and its lockout is short: long enough to keep a
# distributed spray off the CPU, too short to be worth weaponising.
LOGIN_WINDOW = int(os.environ.get("GW_LOGIN_WINDOW_SECONDS", "300"))
LOGIN_MAX_FAILS = int(os.environ.get("GW_LOGIN_MAX_FAILS", "8"))
LOGIN_LOCKOUT = int(os.environ.get("GW_LOGIN_LOCKOUT_SECONDS", "900"))
LOGIN_USER_WINDOW = int(os.environ.get("GW_LOGIN_USER_WINDOW_SECONDS", "900"))
LOGIN_USER_MAX_FAILS = int(os.environ.get("GW_LOGIN_USER_MAX_FAILS", "20"))
LOGIN_USER_LOCKOUT = int(os.environ.get("GW_LOGIN_USER_LOCKOUT_SECONDS", "60"))

# Server limits. Thread-per-connection with no cap is a slowloris target.
MAX_CONNECTIONS = int(os.environ.get("GW_MAX_CONNECTIONS", "128"))
SOCKET_TIMEOUT = int(os.environ.get("GW_SOCKET_TIMEOUT_SECONDS", "15"))

# Passwords that appear in the README. Recognised by name so public mode can
# refuse them explicitly instead of only refusing an unset variable.
DEFAULT_PASSWORDS = {"arise-admin", "arise-dev", "direct-cust"}

# Which tenants an admin may bind a new user to. Single source:
# platform/tenants.yaml, rendered into the platform-tenants ConfigMap and
# mounted here. Hardcoding it meant a newly onboarded customer could not be
# given an account at all — the console would reject their own namespace.
# The literals are the fallback for a pod without the mount, and the thing
# scripts/tenant-check.py compares against the register.
TENANTS_PATH = os.environ.get("TENANTS_PATH", "/etc/arise/tenants.json")
VALID_TENANTS = ["tenant-arise", "tenant-direct"]


def load_tenants():
    """Adopt the mounted register if present. Fails SOFT: a malformed file must
    not take the front door down — the built-ins are a known-good pair, and the
    L0 gate catches a mismatch before it ever deploys."""
    try:
        with open(TENANTS_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        names = sorted(k for k in raw if isinstance(k, str))
        if not names:
            raise ValueError("register lists no tenants")
        VALID_TENANTS[:] = names
        log("INFO", "tenant register loaded", path=TENANTS_PATH,
            tenants=VALID_TENANTS)
    except FileNotFoundError:
        log("INFO", "no tenant register mounted; using built-in defaults",
            path=TENANTS_PATH, tenants=VALID_TENANTS)
    except Exception as exc:                                 # noqa: BLE001
        log("ERROR", "tenant register unreadable; using built-in defaults",
            path=TENANTS_PATH, error_class=type(exc).__name__)

_STATE_LOCK = threading.RLock()


def log(level, msg, **kw):
    rec = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "level": level, "component": "gateway", "msg": msg}
    rec.update(kw)
    print(json.dumps(rec), flush=True)


# ------------------------------- user store ---------------------------------
def _pw_hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120000).hex()


USERS: dict = {}
# Revoked token ids (logout), jti -> expiry. Bounded two ways: entries are
# dropped once the token they belong to would have expired anyway, and each
# user holds at most REVOKED_PER_USER (a login/logout loop cannot grow it).
# PROCESS-LOCAL by design: a rollout forgets it, so a logged-out token is
# valid again for the remainder of its TTL after a restart. That is the
# stateless-token trade-off (review 2026-08-27 P2-8); a shared revocation
# store is decision D3 (identity provider). Keep SESSION_TTL short.
REVOKED: dict = {}
REVOKED_BY_USER: dict = {}          # name -> [jti, ...] oldest first
REVOKED_PER_USER = 64


def add_user(name, password, role, tenant, display, deterministic=False):
    """Create (or replace) a user record.

    `deterministic` is what makes a session portable. A seeded account's
    credential material is derived from the session key + username + password,
    so EVERY replica and every restart computes the same salt, hash and
    identity version — which is exactly what lets a signed token minted before
    a deploy still validate after it (and on a different pod). Random material
    here would silently defeat that: the version is signed into the token, so
    a fresh version per process logs every customer out on every restart.

    Runtime-created users stay random: they live only in this process anyway
    (the identity provider, decision D3, is what replaces that), and random is
    the safer default for anything not derived from committed config.
    """
    if deterministic:
        salt = hmac.new(SESSION_KEY, f"seed-salt:{name}".encode(),
                        hashlib.sha256).digest()[:16]
    else:
        salt = secrets.token_bytes(16)
    pw_hash = _pw_hash(password, salt)
    if deterministic:
        # Derived from the hash, so changing a seed password in the Secret
        # invalidates that account's live sessions on the next rollout — a
        # password rotation must actually revoke.
        ver = hmac.new(SESSION_KEY, f"seed-ver:{name}:{pw_hash}".encode(),
                       hashlib.sha256).hexdigest()[:16]
    else:
        # Fresh per create: deleting and re-creating a username does NOT
        # resurrect the old account's live sessions.
        ver = secrets.token_hex(8)
    with _STATE_LOCK:
        USERS[name] = {"salt": salt, "hash": pw_hash,
                       "role": role, "tenant": tenant, "display": display,
                       "ver": ver}


class SecretsMissing(RuntimeError):
    """Public mode was requested without real credentials."""


def _seed_password(env_name: str, lab_default: str) -> str:
    """One seed password, fail-closed in public mode.

    A default password documented in the README is not a secret. In public mode
    an unset — or still-default — variable stops the process at startup, which
    is loud and safe; the alternative is a public front door with a published
    admin password, which is silent and not.
    """
    val = os.environ.get(env_name)
    if not PUBLIC_MODE:
        return val or lab_default
    if not val:
        raise SecretsMissing(
            f"{env_name} is unset and GW_PUBLIC_MODE=true. Provide real "
            f"credentials (Secret platform-gateway-auth) or run with "
            f"GW_PUBLIC_MODE=false for the prelab.")
    if val in DEFAULT_PASSWORDS:
        raise SecretsMissing(
            f"{env_name} is set to a documented default password and "
            f"GW_PUBLIC_MODE=true. Refusing to serve a public front door with "
            f"a password that is printed in the README.")
    if len(val) < 12:
        raise SecretsMissing(
            f"{env_name} is shorter than 12 characters; public mode requires "
            f"credentials that survive an online guessing attempt.")
    return val


# The three accounts every deploy re-creates deterministically. Deleting one
# is NOT a durable revocation (review 2026-08-27 P1-1): the next rollout
# re-seeds the same salt/hash/ver and a stolen token validates again for the
# rest of its TTL. The only durable revocation for a seed account is rotating
# its password in Secret platform-gateway-auth (lab: the env default).
SEED_ACCOUNTS = ("admin", "arise-dev", "direct-cust")


def seed_users():
    """Seed accounts. Lab: documented defaults. Public: real secrets or death.

    Runtime-created users still live only in this process — that store is
    replaced wholesale by the identity provider (decision D3). What is
    permanent is the ENFORCEMENT below, which an IdP would also front.

    MUST be called after SESSION_KEY is bound: seeded credential material is
    derived from it (see add_user), which is what makes sessions survive a
    restart and lets a second replica validate the first's tokens.
    """
    assert SESSION_KEY, "seed_users() called before SESSION_KEY was bound"
    add_user("admin", _seed_password("GW_ADMIN_PASSWORD", "arise-admin"),
             "admin", None, "平台管理员", deterministic=True)
    add_user("arise-dev", _seed_password("GW_ARISE_PASSWORD", "arise-dev"),
             "user", "tenant-arise", "内部研发", deterministic=True)
    add_user("direct-cust", _seed_password("GW_DIRECT_PASSWORD", "direct-cust"),
             "user", "tenant-direct", "Direct 客户", deterministic=True)


def session_key() -> bytes:
    """HMAC key for session tokens.

    Public mode REQUIRES a stable key from the environment: it is what lets a
    session survive a pod restart and lets more than one replica exist (both
    are launch requirements — today a deploy logs out every paying customer).
    The lab generates an ephemeral one, which is fine for a single pod nobody
    depends on.
    """
    val = os.environ.get("GW_SESSION_KEY")
    if val:
        if PUBLIC_MODE and len(val) < 32:
            raise SecretsMissing(
                "GW_SESSION_KEY must be at least 32 characters in public mode "
                "(it is the only thing standing between a forged cookie and an "
                "admin session).")
        return val.encode()
    if PUBLIC_MODE:
        raise SecretsMissing(
            "GW_SESSION_KEY is unset and GW_PUBLIC_MODE=true. Without a stable "
            "key every restart logs out every customer and no second replica "
            "can validate the first's sessions.")
    return secrets.token_bytes(32)


SESSION_KEY = b""      # bound in main(); tests bind it directly


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(txt: str) -> bytes:
    return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))


def new_session(name: str) -> str:
    """Mint a STATELESS signed session token.

    Stateless because the alternative — the previous in-process SESSIONS dict —
    made every deploy a mass logout and made a second replica impossible. The
    token carries only an identity reference; role/tenant are re-read from the
    user store on every request, so a demotion takes effect immediately rather
    than at the next login.
    """
    with _STATE_LOCK:
        ver = USERS[name]["ver"]
    now = int(time.time())
    payload = {"u": name, "v": ver, "iat": now, "exp": now + SESSION_TTL,
               "jti": secrets.token_hex(8)}
    body = _b64(json.dumps(payload, separators=(",", ":"),
                           sort_keys=True).encode())
    sig = _b64(hmac.new(SESSION_KEY, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def _prune_revoked(now: float) -> None:
    for jti in [j for j, exp in REVOKED.items() if exp < now]:
        REVOKED.pop(jti, None)


def revoke(jti: str, exp: float, user: str = "-") -> None:
    with _STATE_LOCK:
        REVOKED[jti] = exp
        lst = REVOKED_BY_USER.setdefault(user, [])
        lst.append(jti)
        while len(lst) > REVOKED_PER_USER:      # oldest revocation lapses first
            REVOKED.pop(lst.pop(0), None)
        _prune_revoked(time.time())


def read_token(tok: str):
    """Validate a session token. Returns the payload dict or None.

    Every failure mode returns None identically: a bad signature, a forged
    payload, an expired token, a revoked jti, a deleted user, and a recreated
    username whose version no longer matches.
    """
    if not tok or tok.count(".") != 1:
        return None
    body, _, sig = tok.partition(".")
    try:
        want = _b64(hmac.new(SESSION_KEY, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, want):
            return None
        payload = json.loads(_unb64(body))
    except Exception:                                        # noqa: BLE001
        return None
    # A signed payload should always be well-formed, but "should" is not a
    # control: parse defensively so a malformed one is a clean rejection
    # rather than a 500 that tells an attacker they found something.
    if not isinstance(payload, dict):
        return None
    try:
        exp = float(payload["exp"])
        jti = str(payload["jti"])
        user = str(payload["u"])
    except (KeyError, TypeError, ValueError):
        return None
    if exp < time.time():
        return None
    with _STATE_LOCK:
        if jti in REVOKED:
            return None
        u = USERS.get(user)
        if not u or u["ver"] != payload.get("v"):
            return None
    return payload


def check_login(name, password):
    u = USERS.get(name)
    if not u:
        # burn comparable time so absent users are not distinguishable
        _pw_hash(password, b"0" * 16)
        return None
    return u if hmac.compare_digest(u["hash"], _pw_hash(password, u["salt"])) else None


# ------------------------------ login throttle ------------------------------
class LoginThrottle:
    """Sliding-window failure counter with lockout.

    Two instances are used (see LOGIN_* constants): one keyed on source IP with
    a strict policy, one keyed on username with a loose one. Neither key alone
    is enough — per-IP alone lets a botnet spray one account from many
    addresses; per-username alone lets one address walk a user list — and their
    policies differ because only one of them can hurt a bystander.

    A lockout returns 429 BEFORE the PBKDF2 work, so a locked-out attacker
    costs the pod nothing.
    """

    # Both maps are keyed by UNAUTHENTICATED input (a source address, a
    # claimed username), so they must be bounded: an attacker posting logins
    # for random usernames would otherwise grow them without limit — a slow
    # memory exhaustion that needs no valid credential at all. Above this many
    # keys a sweep drops everything that can no longer affect a decision.
    MAX_KEYS = int(os.environ.get("GW_THROTTLE_MAX_KEYS", "4096"))

    def __init__(self, window=LOGIN_WINDOW, max_fails=LOGIN_MAX_FAILS,
                 lockout=LOGIN_LOCKOUT):
        self.window, self.max_fails, self.lockout = window, max_fails, lockout
        self._fails: dict = {}
        self._until: dict = {}
        self._lock = threading.Lock()

    def _sweep(self, now):
        """Drop keys that can no longer change an answer. Caller holds the lock.

        A failure list whose newest entry has aged out of the window is
        equivalent to no entry at all; an expired lockout likewise. Sweeping
        only when the map is large keeps this O(n) pass rare.
        """
        cutoff = now - self.window
        for key in [k for k, hits in self._fails.items()
                    if not hits or hits[-1] <= cutoff]:
            del self._fails[key]
        for key in [k for k, until in self._until.items() if until <= now]:
            del self._until[key]

    def _locked_key(self, key, now):
        until = self._until.get(key, 0)
        if until > now:
            # CEIL, never int(): truncation reported 0 for any lockout with
            # under a second left, and 0 is the caller's "proceed" signal — the
            # lockout would have leaked one free attempt at its own tail.
            return max(1, math.ceil(until - now))
        if until:
            self._until.pop(key, None)
        return 0

    def retry_after(self, keys, now=None):
        """Seconds to wait, or 0 if the attempt may proceed."""
        now = time.time() if now is None else now
        with self._lock:
            return max((self._locked_key(k, now) for k in keys), default=0)

    def record_failure(self, keys, now=None):
        now = time.time() if now is None else now
        with self._lock:
            if len(self._fails) + len(self._until) > self.MAX_KEYS:
                self._sweep(now)
            for key in keys:
                hits = [h for h in self._fails.get(key, []) if h > now - self.window]
                hits.append(now)
                self._fails[key] = hits
                if len(hits) >= self.max_fails:
                    self._until[key] = now + self.lockout
                    self._fails.pop(key, None)

    def record_success(self, keys):
        with self._lock:
            for key in keys:
                self._fails.pop(key, None)


# Strict: the key is the attacker's own address.
THROTTLE_IP = LoginThrottle(LOGIN_WINDOW, LOGIN_MAX_FAILS, LOGIN_LOCKOUT)
# Loose + short: the key is a victim's account name.
THROTTLE_USER = LoginThrottle(LOGIN_USER_WINDOW, LOGIN_USER_MAX_FAILS,
                              LOGIN_USER_LOCKOUT)


def login_retry_after(ip: str, user: str) -> int:
    """Seconds the caller must wait, 0 if the attempt may proceed."""
    return max(THROTTLE_IP.retry_after((f"ip:{ip}",)),
               THROTTLE_USER.retry_after((f"user:{user}",)))


def login_record_failure(ip: str, user: str) -> None:
    THROTTLE_IP.record_failure((f"ip:{ip}",))
    THROTTLE_USER.record_failure((f"user:{user}",))


def login_record_success(ip: str, user: str) -> None:
    THROTTLE_IP.record_success((f"ip:{ip}",))
    THROTTLE_USER.record_success((f"user:{user}",))


def session_user(handler):
    """Resolve the session cookie to a live user record, or None."""
    raw = handler.headers.get("Cookie", "")
    tok = None
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            tok = v
    payload = read_token(tok) if tok else None
    if not payload:
        return None
    u = USERS.get(payload["u"])
    if not u:
        return None
    return {"name": payload["u"], "role": u["role"], "tenant": u["tenant"],
            "display": u["display"], "_jti": payload["jti"],
            "_exp": payload["exp"]}


def public_user(name):
    u = USERS[name]
    return {"name": name, "role": u["role"], "tenant": u["tenant"],
            "display": u["display"]}


# --------------------------------- handler ----------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "arise-gateway/2.0"

    # BaseHTTPRequestHandler's own stderr logging stays off; _access() below
    # emits the structured line instead (same JSON shape as every other log).
    def log_message(self, fmt, *args):
        pass

    def client_ip(self):
        """Best available client address.

        X-Forwarded-For is client-controlled unless something trusted rewrote
        it, so it is honoured only when the deployment declares an edge in
        front (GW_TRUST_PROXY). The LEFTMOST entry is the original client as
        appended by that edge; without the flag the socket peer is the only
        honest answer. Rate limiting and audit both key on this, so guessing
        wrong here would let an attacker forge their way out of a lockout.
        """
        if TRUST_PROXY:
            xff = self.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[0].strip()[:64]
        try:
            return self.client_address[0]
        except Exception:                                    # noqa: BLE001
            return "-"

    def _access(self, code, **kw):
        """One structured access line per request — the forensic trail a public
        service needs. Health probes and cached asset hits are skipped so the
        signal is not buried; every auth event and every non-2xx is kept."""
        path = urllib.parse.urlparse(self.path).path
        if path in ("/healthz", "/readyz"):
            return
        if code < 400 and path.startswith("/assets/"):
            return
        log("INFO" if code < 400 else "WARN", "request",
            method=self.command, path=path[:200], status=code,
            ip=self.client_ip(), ua=(self.headers.get("User-Agent") or "-")[:80],
            **kw)

    def _csrf_ok(self):
        """Reject cross-site state-changing requests.

        Browsers attach Origin to every POST/DELETE, including same-origin
        ones, so comparing it to Host catches the classic cross-site form/fetch
        POST while leaving the SPA (same origin by construction) untouched. A
        request with no Origin at all is a non-browser client and is left to the
        session cookie check. SameSite=Lax on the cookie is the second layer.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        try:
            return urllib.parse.urlparse(origin).netloc == host
        except Exception:                                    # noqa: BLE001
            return False

    def _begin_request(self):
        """Reset per-REQUEST state.

        A handler instance is per-CONNECTION, not per-request: with HTTP/1.1
        keep-alive one instance serves every request on that socket. So a flag
        left set by request N is read as truth by request N+1 — which for
        _body_consumed would mean the desync guard below silently stops firing
        after the connection's first successful POST. Every do_* entry point
        calls this first.
        """
        self._body_consumed = False

    def _close_if_body_unread(self):
        """HTTP/1.1 keep-alive correctness.

        If the client DECLARED a body we never read (the 413 cap and the CSRF
        refusal both answer before reading), those unread bytes would be parsed
        as the start of the next request on this connection — turning an
        attacker-controlled body into an attacker-controlled request. Closing
        the connection is the only safe answer; draining an oversized body is
        exactly what the cap exists to avoid.
        """
        declared = int(self.headers.get("Content-Length") or 0)
        if declared and not getattr(self, "_body_consumed", False):
            self.close_connection = True

    def _cookie(self, tok, expire=False):
        """Session cookie with the strongest attributes the deployment allows.

        HttpOnly: JS cannot read it, so an XSS cannot exfiltrate the session.
        SameSite=Lax: not sent on cross-site POSTs (CSRF layer 2).
        Secure + __Host- prefix (public mode): the browser refuses to send it
        over plain HTTP and refuses to accept it from a sibling host or a
        narrower path — which is what makes cookie planting hard.
        """
        parts = [f"{COOKIE}={tok}", "HttpOnly", "Path=/", "SameSite=Lax"]
        if COOKIE_SECURE:
            parts.append("Secure")
        parts.append("Max-Age=0" if expire else f"Max-Age={SESSION_TTL}")
        return "; ".join(parts)

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
        if COOKIE_SECURE:
            # Only meaningful once TLS terminates in front; harmless otherwise,
            # and it must be present from the first response a browser sees.
            self.send_header("Strict-Transport-Security",
                             "max-age=31536000; includeSubDomains")
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
        self._close_if_body_unread()
        self._access(code)

    def _refuse_body_on_bodyless(self):
        """GET/DELETE never read a body, so a Content-Length (or chunked) body
        would stay on the socket and prefix the NEXT request on this kept-alive
        connection — the same smuggling shape as an unread chunked POST.
        Closing after the response is enough: nothing is ever parsed from it."""
        if self.headers.get("Transfer-Encoding") or int(self.headers.get("Content-Length") or 0) > 0:
            self.close_connection = True

    def _read_body(self):
        """Request body, hard-capped at MAX_BODY. Sends 413 and returns None on
        overrun so a huge/lying Content-Length can never allocate unbounded
        memory (unauthenticated on the login path).

        Transfer-Encoding is refused outright (411 + close). This server frames
        every body by Content-Length; a chunked body it never read would stay
        on the socket and become the prefix of the NEXT request on a kept-alive
        upstream connection — request smuggling, held closed today only by
        ingress-nginx's default body buffering (review 2026-08-27 P2-9)."""
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._send(411, {"error": "Transfer-Encoding is not accepted; "
                                      "send a Content-Length"})
            return None
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            # Deliberately NOT consumed: reading it is exactly what the cap
            # exists to avoid. _send() closes the connection instead.
            self._send(413, {"error": "request body too large"})
            return None
        raw = self.rfile.read(n) if n > 0 else b""
        self._body_consumed = True
        return raw

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
            name = str(b.get("username", ""))[:64]
            ip = self.client_ip()
            # Throttle check FIRST: a locked-out attempt must never reach the
            # PBKDF2 work, or the lockout becomes a CPU amplifier.
            wait = login_retry_after(ip, name)
            if wait:
                log("WARN", "login throttled", user=name, ip=ip,
                    retry_after=wait)
                self._send(429, {"error": "too many failed attempts; try again "
                                          f"in {wait}s"},
                           extra_headers=[("Retry-After", str(wait))])
                return True
            u = check_login(name, str(b.get("password", "")))
            if not u:
                login_record_failure(ip, name)
                log("WARN", "login failed", user=name, ip=ip)
                self._send(401, {"error": "用户名或密码错误 / bad credentials"})
                return True
            login_record_success(ip, name)
            tok = new_session(name)
            log("INFO", "login", user=name, role=u["role"], ip=ip)
            self._send(200, public_user(name),
                       extra_headers=[("Set-Cookie", self._cookie(tok))])
            return True

        me = session_user(self)
        if path == "/auth/me" and method == "GET":
            self._send(200 if me else 401,
                       public_user(me["name"]) if me else {"error": "unauthenticated"})
            return True
        if path == "/auth/logout" and method == "POST":
            if me:
                # Stateless tokens cannot be forgotten, so logout records the
                # token id until its own expiry — a bounded set, unlike the old
                # session store which grew with every login.
                revoke(me["_jti"], me["_exp"], me["name"])
                log("INFO", "logout", user=me["name"], ip=self.client_ip())
            self._send(200, {"ok": True},
                       extra_headers=[("Set-Cookie", self._cookie("", expire=True))])
            return True

        # ---- self-service password change ----------------------------------
        if path == "/auth/password" and method == "POST":
            if not me:
                self._send(401, {"error": "unauthenticated"})
                return True
            body = self._body_json()
            if body is None:
                return True
            cur = str(body.get("current") or "")
            new_pw = str(body.get("new") or "")
            if me["name"] in SEED_ACCOUNTS:
                # A seeded account's credential is DERIVED from the Secret on
                # every start: a change made here would be silently undone by
                # the next rollout — and the customer would be locked out.
                # Honest answer: this one is rotated by ops, in the Secret.
                self._send(400, {"error": (
                    f"{me['name']} is a provisioned account; its password is "
                    "rotated by ARISE ops (Secret platform-gateway-auth) — "
                    "ask support, you will get a new credential")})
                return True
            if not check_login(me["name"], cur):
                log("WARN", "password change refused (current mismatch)",
                    user=me["name"], ip=self.client_ip())
                self._send(403, {"error": "current password does not match"})
                return True
            if len(new_pw) < 12 or new_pw == cur:
                self._send(400, {"error": "new password must be at least 12 characters and different"})
                return True
            u = USERS[me["name"]]
            add_user(me["name"], new_pw, u["role"], u["tenant"], u["display"])  # fresh salt + ver
            log("INFO", "password changed", user=me["name"], ip=self.client_ip())
            # Every live session (this one included) carries the OLD ver and
            # is now invalid: that is what a password change must mean.
            self._send(200, {"ok": True, "note": "all sessions invalidated; log in again"},
                       extra_headers=[("Set-Cookie", self._cookie("", expire=True))])
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
                elif role == "user" and tenant not in VALID_TENANTS:
                    self._send(400, {"error": "a user needs a tenant; valid: "
                                              + ", ".join(VALID_TENANTS)})
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
                elif name in SEED_ACCOUNTS:
                    self._send(400, {"error": (
                        f"{name} is a seeded account: deleting it is undone by "
                        "the next rollout, which would re-validate a stolen "
                        "session. To revoke it durably, rotate its password in "
                        "Secret platform-gateway-auth and restart the gateway.")})
                elif name not in USERS:
                    self._send(404, {"error": "no such user"})
                elif USERS[name]["role"] == "admin" and \
                        sum(1 for u in USERS.values() if u["role"] == "admin") == 1:
                    self._send(400, {"error": "cannot delete the last admin"})
                else:
                    # Removing the record invalidates every live token for that
                    # identity: read_token() re-reads USERS on every request and
                    # returns None when the user (or its version) is gone. No
                    # session sweep is needed, and none can be forgotten.
                    with _STATE_LOCK:
                        USERS.pop(name, None)
                    log("INFO", "user deleted", user=name, by=me["name"],
                        ip=self.client_ip())
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
        self._begin_request()
        self._refuse_body_on_bodyless()
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
        self._begin_request()
        if not self._csrf_ok():
            log("WARN", "cross-origin write refused", ip=self.client_ip(),
                origin=(self.headers.get("Origin") or "-")[:80])
            self._send(403, {"error": "cross-origin request refused"})
            return
        path = urllib.parse.urlparse(self.path).path
        if self._auth("POST", path):
            return
        self._proxy("POST")

    def do_PUT(self):                                        # noqa: N802
        self._begin_request()
        # Same gate as POST: Origin check, session, then the tenant-scoped
        # proxy (the portal's PUT .../ssh-key key rotation, 2026-08-27).
        if not self._csrf_ok():
            log("WARN", "cross-origin write refused", ip=self.client_ip(),
                origin=(self.headers.get("Origin") or "-")[:80])
            self._send(403, {"error": "cross-origin request refused"})
            return
        path = urllib.parse.urlparse(self.path).path
        if self._auth("PUT", path):
            return
        self._proxy("PUT")

    def do_DELETE(self):                                     # noqa: N802
        self._begin_request()
        self._refuse_body_on_bodyless()
        if not self._csrf_ok():
            log("WARN", "cross-origin write refused", ip=self.client_ip(),
                origin=(self.headers.get("Origin") or "-")[:80])
            self._send(403, {"error": "cross-origin request refused"})
            return
        path = urllib.parse.urlparse(self.path).path
        if self._auth("DELETE", path):
            return
        self._proxy("DELETE")



class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a hard ceiling on concurrent connections.

    The stock server spawns one unbounded thread per connection, so a client
    that opens sockets and never speaks (slowloris) can exhaust memory in a pod
    capped at 128Mi long before it exhausts anyone's patience. Connections over
    the ceiling are closed immediately rather than queued, which keeps the
    failure mode "some clients are refused" instead of "the pod dies".
    """

    daemon_threads = True          # no lingering threads on shutdown
    request_queue_size = 64

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            log("WARN", "connection refused: at capacity",
                limit=MAX_CONNECTIONS, ip=client_address[0])
            # close_request(), NOT shutdown_request(): the latter is overridden
            # below to release a slot, and releasing one we never acquired
            # would quietly raise the effective ceiling on every refusal.
            try:
                request.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            self.close_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            # The worker thread never started, so shutdown_request() will not
            # run for this connection: hand the slot back here.
            self._slots.release()
            raise

    def shutdown_request(self, request):
        # Called exactly once per connection that reached a worker thread.
        try:
            super().shutdown_request(request)
        finally:
            self._slots.release()


def main():
    global SESSION_KEY
    load_tenants()           # before seeding: accounts bind to these names
    try:
        SESSION_KEY = session_key()
        seed_users()
    except SecretsMissing as exc:
        # Fail CLOSED and loudly. A public front door that boots with a
        # README password is worse than one that does not boot.
        log("ERROR", "refusing to start", detail=str(exc), public_mode=PUBLIC_MODE)
        return 1
    log("INFO", "gateway listening", port=PORT, backends=list(BACKENDS),
        users=len(USERS), public_mode=PUBLIC_MODE, cookie_secure=COOKIE_SECURE,
        trust_proxy=TRUST_PROXY, max_connections=MAX_CONNECTIONS,
        note="stateless signed sessions; role enforcement at the proxy; "
             "no k8s credentials")
    # Per-connection read timeout: an idle or trickling client is dropped
    # instead of holding a thread forever.
    Handler.timeout = SOCKET_TIMEOUT
    Handler.protocol_version = "HTTP/1.1"   # every response sets Content-Length
    srv = BoundedThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
