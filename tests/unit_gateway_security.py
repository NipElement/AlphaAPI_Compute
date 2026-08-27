#!/usr/bin/env python3
"""Unit tests for the gateway's public-mode security surface — no cluster.

Wired into scripts/validate.sh as an L0 gate. Covers the WS2 changes:
  - fail-closed seeding: public mode refuses unset / README-default / short
    passwords and an unset-or-weak GW_SESSION_KEY; lab mode still boots
  - stateless signed sessions: round-trip, tamper, forge, expiry, revocation,
    deleted user, recreated-username (identity version)
  - login throttle: per-IP and per-username windows, lockout, reset on success
  - CSRF origin check and cookie attributes
  - the connection-limiter's semaphore accounting

The module is imported fresh per environment because its posture constants are
read at import time (that is the point: posture is deployment config, not a
runtime toggle an attacker could flip).
"""
import importlib.util
import os
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "services" / "gateway" / "gateway.py"
FAILS = []


def load(init=False, **env):
    """Import gateway.py under a given environment. Returns the module.

    Posture constants (PUBLIC_MODE, COOKIE, ...) are bound at IMPORT time, but
    session_key() and _seed_password() read os.environ when CALLED — so
    `init=True` runs startup INSIDE the environment, exactly as main() does.
    Startup failures propagate to the caller, which is what the fail-closed
    tests assert on.
    """
    saved = {k: os.environ.get(k) for k in (
        "GW_PUBLIC_MODE", "GW_COOKIE_SECURE", "GW_TRUST_PROXY", "GW_SESSION_KEY",
        "GW_ADMIN_PASSWORD", "GW_ARISE_PASSWORD", "GW_DIRECT_PASSWORD")}
    for k in saved:
        os.environ.pop(k, None)
    os.environ.update({k: v for k, v in env.items() if v is not None})
    try:
        spec = importlib.util.spec_from_file_location("gw_under_test", SRC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if init:                       # same order as main()
            mod.SESSION_KEY = mod.session_key()
            mod.seed_users()
        return mod
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def check(name, fn):
    try:
        fn()
        print(f"  ok   {name}")
    except AssertionError as exc:
        FAILS.append(name)
        print(f"  FAIL {name}: {exc}")
    except Exception as exc:                                  # noqa: BLE001
        FAILS.append(name)
        print(f"  FAIL {name}: unexpected {type(exc).__name__}: {exc}")


GOOD = {"GW_ADMIN_PASSWORD": "a-real-admin-secret-2026",
        "GW_ARISE_PASSWORD": "another-real-secret-2026",
        "GW_DIRECT_PASSWORD": "third-real-secret-2026",
        "GW_SESSION_KEY": "x" * 48}


def public_mod():
    """A fully-configured public-mode module with sessions ready."""
    return load(init=True, GW_PUBLIC_MODE="true", **GOOD)


# ------------------------------------------------- fail-closed seeding -----

def t_lab_mode_boots_with_defaults():
    gw = load(init=True)
    assert set(gw.USERS) == {"admin", "arise-dev", "direct-cust"}, gw.USERS.keys()
    assert gw.check_login("admin", "arise-admin"), "lab default must still work"
    assert gw.COOKIE == "arise_session", "lab cookie is unprefixed"


def t_public_refuses_unset_password():
    try:
        load(init=True, GW_PUBLIC_MODE="true", GW_SESSION_KEY="x" * 48)
    except RuntimeError as exc:       # SecretsMissing subclasses RuntimeError
        assert "unset" in str(exc), str(exc)
        return
    raise AssertionError("public mode must refuse an unset seed password")


def t_public_refuses_readme_default():
    try:
        load(init=True, GW_PUBLIC_MODE="true", GW_SESSION_KEY="x" * 48,
             GW_ADMIN_PASSWORD="arise-admin",
             GW_ARISE_PASSWORD="another-real-secret-2026",
             GW_DIRECT_PASSWORD="third-real-secret-2026")
    except RuntimeError as exc:
        assert "default password" in str(exc), str(exc)
        return
    raise AssertionError("public mode must refuse a documented default password")


def t_public_refuses_short_password():
    try:
        load(init=True, GW_PUBLIC_MODE="true", GW_SESSION_KEY="x" * 48,
             GW_ADMIN_PASSWORD="short1", GW_ARISE_PASSWORD="a" * 20,
             GW_DIRECT_PASSWORD="b" * 20)
    except RuntimeError as exc:
        assert "12 characters" in str(exc), str(exc)
        return
    raise AssertionError("public mode must refuse a short password")


def t_public_refuses_missing_session_key():
    try:
        load(init=True, GW_PUBLIC_MODE="true",
             **{k: v for k, v in GOOD.items() if k != "GW_SESSION_KEY"})
    except RuntimeError as exc:
        assert "GW_SESSION_KEY" in str(exc), str(exc)
        return
    raise AssertionError("public mode must refuse an unset GW_SESSION_KEY")


def t_public_refuses_weak_session_key():
    try:
        load(init=True, GW_PUBLIC_MODE="true",
             **{**GOOD, "GW_SESSION_KEY": "tooshort"})
    except RuntimeError as exc:
        assert "32 characters" in str(exc), str(exc)
        return
    raise AssertionError("public mode must refuse a short GW_SESSION_KEY")


def t_public_cookie_is_host_prefixed():
    gw = public_mod()
    assert gw.COOKIE == "__Host-arise_session", gw.COOKIE
    assert gw.COOKIE_SECURE is True


# ------------------------------------------------- stateless sessions ------

def t_token_round_trip():
    gw = public_mod()
    tok = gw.new_session("admin")
    payload = gw.read_token(tok)
    assert payload and payload["u"] == "admin", payload
    assert payload["exp"] > time.time(), "token must be live"


def t_token_survives_restart_with_same_key():
    """The launch requirement: a deploy must not log every customer out."""
    gw1 = public_mod()
    tok = gw1.new_session("direct-cust")
    gw2 = public_mod()                    # fresh process, same GW_SESSION_KEY
    assert gw2.read_token(tok), "a restart with a stable key must keep sessions"


def t_seed_password_rotation_revokes_sessions():
    """Rotating a password in the Secret must actually log that account out."""
    gw1 = public_mod()
    tok = gw1.new_session("admin")
    gw2 = load(init=True, GW_PUBLIC_MODE="true",
               **{**GOOD, "GW_ADMIN_PASSWORD": "rotated-admin-secret-2026"})
    assert gw2.read_token(tok) is None, \
        "a rotated seed password left the old sessions valid"
    # ...while an untouched account keeps its sessions across that same roll.
    other = gw1.new_session("direct-cust")
    assert gw2.read_token(other), \
        "rotating one account must not log out the others"


def t_token_dies_with_different_key():
    gw1 = public_mod()
    tok = gw1.new_session("admin")
    gw2 = load(init=True, GW_PUBLIC_MODE="true",
               **{**GOOD, "GW_SESSION_KEY": "y" * 48})
    assert gw2.read_token(tok) is None, "a token from another key must not validate"


def t_token_tamper_rejected():
    gw = public_mod()
    body, _, sig = gw.new_session("arise-dev").partition(".")
    # Re-sign nothing: swap the payload for an admin one, keep the old signature.
    forged_body = gw._b64(b'{"exp":9999999999,"iat":0,"jti":"deadbeef",'
                          b'"u":"admin","v":"x"}')
    assert gw.read_token(f"{forged_body}.{sig}") is None, "forged payload accepted"
    assert gw.read_token(f"{body}.{'A' * len(sig)}") is None, "bad signature accepted"
    assert gw.read_token("garbage") is None
    assert gw.read_token("") is None


def t_wellformed_signature_but_garbage_payload():
    """A correctly SIGNED but malformed payload must reject cleanly, not raise.

    Only reachable if the key leaks, but a 500 there tells an attacker they
    found something; a plain 401 tells them nothing.
    """
    gw = public_mod()
    import hashlib as _h, hmac as _hm
    for junk in (b'{"exp":"soon","jti":"x","u":"admin","v":"y"}',
                 b'{"u":"admin"}', b'[]', b'"string"', b'{}'):
        body = gw._b64(junk)
        sig = gw._b64(_hm.new(gw.SESSION_KEY, body.encode(), _h.sha256).digest())
        assert gw.read_token(f"{body}.{sig}") is None, f"accepted junk: {junk!r}"


def t_token_expiry_enforced():
    gw = public_mod()
    gw.SESSION_TTL = -1                    # mint an already-expired token
    tok = gw.new_session("admin")
    assert gw.read_token(tok) is None, "expired token accepted"


def t_revocation_and_pruning():
    gw = public_mod()
    tok = gw.new_session("admin")
    payload = gw.read_token(tok)
    gw.revoke(payload["jti"], payload["exp"])
    assert gw.read_token(tok) is None, "revoked token still valid"
    # A revocation entry for an already-expired token is pruned, so the set
    # stays bounded rather than growing with every logout forever.
    gw.revoke("stale-jti", time.time() - 10)
    gw.revoke("fresh-jti", time.time() + 600)
    assert "stale-jti" not in gw.REVOKED, "expired revocation not pruned"


def t_deleted_user_tokens_die():
    gw = public_mod()
    tok = gw.new_session("direct-cust")
    assert gw.read_token(tok), "precondition"
    gw.USERS.pop("direct-cust")
    assert gw.read_token(tok) is None, "deleted user's session survived"


def t_recreated_username_does_not_resurrect_sessions():
    gw = public_mod()
    tok = gw.new_session("direct-cust")
    gw.USERS.pop("direct-cust")
    gw.add_user("direct-cust", "brand-new-secret-2026", "user",
                "tenant-direct", "Direct 客户")
    assert gw.read_token(tok) is None, \
        "a recreated username must not inherit the old account's sessions"


# ------------------------------------------------------ login throttle -----

def t_throttle_locks_after_max_fails():
    gw = public_mod()
    th = gw.LoginThrottle(window=300, max_fails=3, lockout=900)
    keys = ("ip:1.2.3.4", "user:admin")
    assert th.retry_after(keys) == 0, "clean state must allow"
    for _ in range(3):
        th.record_failure(keys)
    assert th.retry_after(keys) > 0, "lockout did not engage"


def t_throttle_success_resets():
    gw = public_mod()
    th = gw.LoginThrottle(window=300, max_fails=3, lockout=900)
    keys = ("ip:1.2.3.4", "user:admin")
    th.record_failure(keys)
    th.record_failure(keys)
    th.record_success(keys)
    th.record_failure(keys)
    assert th.retry_after(keys) == 0, "counter did not reset on success"


def t_throttle_window_slides():
    gw = public_mod()
    th = gw.LoginThrottle(window=60, max_fails=3, lockout=900)
    keys = ("ip:1.2.3.4",)
    old = time.time() - 120                # outside the window
    th.record_failure(keys, now=old)
    th.record_failure(keys, now=old)
    th.record_failure(keys)                # only 1 inside the window
    assert th.retry_after(keys) == 0, "stale failures must age out"


def t_throttle_keys_are_independent():
    """A locked-out username must not lock out an unrelated IP, and vice versa."""
    gw = public_mod()
    th = gw.LoginThrottle(window=300, max_fails=2, lockout=900)
    for _ in range(2):
        th.record_failure(("ip:9.9.9.9", "user:victim"))
    assert th.retry_after(("ip:9.9.9.9",)) > 0, "attacker IP not locked"
    assert th.retry_after(("user:victim",)) > 0, "sprayed account not locked"
    assert th.retry_after(("ip:10.0.0.1", "user:someone-else")) == 0, \
        "an unrelated client was collaterally locked out"


def t_throttle_map_is_bounded_against_unauthenticated_growth():
    """Login keys come from unauthenticated input.

    Posting logins for endless random usernames must not grow the throttle
    maps without limit — that is memory exhaustion needing no credential. Keys
    that can no longer affect a decision (failures aged out of the window,
    expired lockouts) are swept once the map gets large.
    """
    gw = public_mod()
    th = gw.LoginThrottle(window=60, max_fails=100, lockout=900)
    th.MAX_KEYS = 50
    stale = time.time() - 600                 # far outside the window
    for i in range(200):
        th.record_failure((f"user:victim-{i}",), now=stale)
    # One more failure at "now" trips the sweep; everything stale is dropped.
    th.record_failure(("user:real",))
    assert len(th._fails) < 60, \
        f"throttle map kept {len(th._fails)} dead keys — unbounded growth"
    # The live key survives the sweep and still counts.
    assert "user:real" in th._fails, "the sweep dropped a live key"


def t_sweep_never_drops_an_active_lockout():
    gw = public_mod()
    th = gw.LoginThrottle(window=60, max_fails=1, lockout=900)
    th.MAX_KEYS = 1
    th.record_failure(("ip:198.51.100.9",))       # locks that IP
    for i in range(20):                            # force sweeps
        th.record_failure((f"user:noise-{i}",), now=time.time() - 600)
        th.record_failure((f"user:live-{i}",))
    assert th.retry_after(("ip:198.51.100.9",)) > 0, \
        "the sweep released an active lockout"


def t_ip_policy_is_stricter_than_username_policy():
    """The two keys must not share a policy.

    A lockout on the attacker's own IP costs only the attacker, so it is
    strict. A lockout on a USERNAME is collateral: anyone who knows an account
    name could otherwise keep that customer out indefinitely — our defence
    weaponised as their denial of service. So the username key must tolerate
    more failures and release far sooner.
    """
    gw = public_mod()
    assert gw.LOGIN_USER_MAX_FAILS > gw.LOGIN_MAX_FAILS, \
        "username key must tolerate more failures than the IP key"
    assert gw.LOGIN_USER_LOCKOUT < gw.LOGIN_LOCKOUT, \
        "a username lockout must release much sooner than an IP lockout"


def t_login_helpers_lock_ip_without_locking_victim():
    """An attacker burning their own IP must not lock the account they target
    for anyone else — the account key has its own, looser budget."""
    gw = public_mod()
    gw.THROTTLE_IP = gw.LoginThrottle(300, 3, 900)
    gw.THROTTLE_USER = gw.LoginThrottle(900, 20, 60)
    for _ in range(3):
        gw.login_record_failure("198.51.100.7", "admin")
    assert gw.login_retry_after("198.51.100.7", "admin") > 0, "attacker IP not locked"
    # The victim, from their own address, is still allowed in.
    assert gw.login_retry_after("203.0.113.5", "admin") == 0, \
        "an attacker locked the victim's account from a third-party address"


def t_login_success_clears_both_keys():
    gw = public_mod()
    gw.THROTTLE_IP = gw.LoginThrottle(300, 3, 900)
    gw.THROTTLE_USER = gw.LoginThrottle(900, 3, 60)
    gw.login_record_failure("198.51.100.7", "admin")
    gw.login_record_failure("198.51.100.7", "admin")
    gw.login_record_success("198.51.100.7", "admin")
    gw.login_record_failure("198.51.100.7", "admin")
    assert gw.login_retry_after("198.51.100.7", "admin") == 0, \
        "a successful login did not clear the failure counters"


def t_throttle_lockout_expires():
    gw = public_mod()
    th = gw.LoginThrottle(window=300, max_fails=1, lockout=1)
    keys = ("ip:1.2.3.4",)
    th.record_failure(keys)
    assert th.retry_after(keys) > 0
    assert th.retry_after(keys, now=time.time() + 5) == 0, "lockout never expires"


# --------------------------------------------------- csrf + cookie ---------

class FakeHandler:
    """Minimal stand-in exposing what the pure handler helpers read."""

    def __init__(self, gw, headers, peer="203.0.113.9"):
        self.headers = headers
        self.client_address = (peer, 12345)
        self.close_connection = False
        for m in ("_cookie", "_csrf_ok", "client_ip", "_begin_request",
                  "_close_if_body_unread"):
            setattr(self, m, getattr(gw.Handler, m).__get__(self, FakeHandler))


def t_csrf_blocks_cross_origin_write():
    gw = public_mod()
    h = FakeHandler(gw, {"Origin": "https://evil.example", "Host": "console.arise.ai"})
    assert h._csrf_ok() is False, "cross-origin write was allowed"


def t_csrf_allows_same_origin_and_no_origin():
    gw = public_mod()
    same = FakeHandler(gw, {"Origin": "https://console.arise.ai",
                            "Host": "console.arise.ai"})
    assert same._csrf_ok() is True, "same-origin write was refused"
    none = FakeHandler(gw, {"Host": "console.arise.ai"})
    assert none._csrf_ok() is True, "non-browser client refused"


def t_cookie_attributes_public_vs_lab():
    gw = public_mod()
    c = FakeHandler(gw, {})._cookie("tok123")
    for attr in ("__Host-arise_session=tok123", "HttpOnly", "Path=/",
                 "SameSite=Lax", "Secure"):
        assert attr in c, f"{attr} missing from {c}"
    expired = FakeHandler(gw, {})._cookie("", expire=True)
    assert "Max-Age=0" in expired, expired

    lab = load(init=True)
    lc = FakeHandler(lab, {})._cookie("tok123")
    assert "Secure" not in lc, "lab (plain HTTP) cookie must not be Secure"
    assert lc.startswith("arise_session="), lc


def t_unread_body_closes_the_connection():
    """A 413 or CSRF refusal must not leave unread bytes on a keep-alive
    connection — they would be parsed as the next request."""
    gw = public_mod()
    h = FakeHandler(gw, {"Content-Length": "9000"})
    h._begin_request()                     # body NOT read (the 413 path)
    h._close_if_body_unread()
    assert h.close_connection is True, "unread body left the connection open"

    ok = FakeHandler(gw, {"Content-Length": "12"})
    ok._begin_request()
    ok._body_consumed = True               # body was read normally
    ok._close_if_body_unread()
    assert ok.close_connection is False, "a consumed body should keep-alive"

    bodyless = FakeHandler(gw, {})
    bodyless._begin_request()
    bodyless._close_if_body_unread()
    assert bodyless.close_connection is False, "no body declared, nothing to drain"


def t_body_consumed_flag_resets_between_requests():
    """The handler instance is per-CONNECTION.

    Without a per-request reset, request N's successful body read would make
    request N+1's 413/CSRF refusal skip the close — the desync guard would
    silently stop working after the first POST on any connection.
    """
    gw = public_mod()
    h = FakeHandler(gw, {"Content-Length": "12"})
    h._begin_request()
    h._body_consumed = True                # request N: read its body fine
    # request N+1 on the SAME instance: oversized body, never read
    h.headers = {"Content-Length": "9000"}
    h._begin_request()
    h._close_if_body_unread()
    assert h.close_connection is True, \
        "stale _body_consumed from a previous request disabled the guard"


def t_entry_points_actually_reset_state():
    """Wiring guard: the helper above is useless if an entry point forgets it.

    Asserted on the source because the reset must be the FIRST statement — any
    _send() before it (an early 403/413) would read a stale flag.
    """
    src = SRC.read_text()
    for verb in ("do_GET", "do_POST", "do_DELETE"):
        assert f"def {verb}(self)" in src, f"{verb} missing"
        body = src.split(f"def {verb}(self)", 1)[1].splitlines()[1:]
        first = next(line.strip() for line in body if line.strip())
        assert first == "self._begin_request()", \
            f"{verb} must reset per-request state first, found: {first!r}"


def t_client_ip_trusts_xff_only_when_configured():
    untrusted = load(init=True)
    h = FakeHandler(untrusted, {"X-Forwarded-For": "1.1.1.1, 2.2.2.2"})
    assert h.client_ip() == "203.0.113.9", \
        "spoofable X-Forwarded-For honoured without GW_TRUST_PROXY"
    trusted = load(init=True, GW_TRUST_PROXY="true")
    h2 = FakeHandler(trusted, {"X-Forwarded-For": "1.1.1.1, 2.2.2.2"})
    assert h2.client_ip() == "1.1.1.1", "edge-supplied client IP ignored"


# ------------------------------------------------ connection limiter -------

def t_connection_slots_are_conserved():
    """A refused connection must not raise the effective ceiling.

    The first cut released a slot it never acquired on the refuse path, so
    every refusal below the ceiling silently widened the limit.
    """
    gw = public_mod()

    class Probe(gw.BoundedThreadingHTTPServer):
        def __init__(self):                       # no socket, no bind
            self._slots = __import__("threading").BoundedSemaphore(2)
            self.closed = 0

        def close_request(self, request):
            self.closed += 1

        def shutdown_request(self, request):
            try:
                pass
            finally:
                self._slots.release()

    class Sock:
        def shutdown(self, how):
            pass

    srv = Probe()
    assert srv._slots.acquire(blocking=False)
    assert srv._slots.acquire(blocking=False)
    # At capacity: this must be refused and must NOT hand back a slot.
    gw.BoundedThreadingHTTPServer.process_request(srv, Sock(), ("10.0.0.1", 1))
    assert srv.closed == 1, "refused connection was not closed"
    assert srv._slots.acquire(blocking=False) is False, \
        "refusing a connection released a slot that was never acquired"
    srv.shutdown_request(Sock())              # a real connection finishing
    assert srv._slots.acquire(blocking=False), "finished connection freed no slot"


checks = [
    ("lab mode boots with documented defaults", t_lab_mode_boots_with_defaults),
    ("public: unset seed password refuses start", t_public_refuses_unset_password),
    ("public: README default password refuses start", t_public_refuses_readme_default),
    ("public: short password refuses start", t_public_refuses_short_password),
    ("public: missing GW_SESSION_KEY refuses start", t_public_refuses_missing_session_key),
    ("public: weak GW_SESSION_KEY refuses start", t_public_refuses_weak_session_key),
    ("public: cookie is __Host- prefixed + Secure", t_public_cookie_is_host_prefixed),
    ("session token round-trips", t_token_round_trip),
    ("session survives restart with stable key", t_token_survives_restart_with_same_key),
    ("session dies under a different key", t_token_dies_with_different_key),
    ("seed password rotation revokes that account", t_seed_password_rotation_revokes_sessions),
    ("forged/tampered tokens rejected", t_token_tamper_rejected),
    ("expired token rejected", t_token_expiry_enforced),
    ("signed-but-malformed payload rejects cleanly", t_wellformed_signature_but_garbage_payload),
    ("logout revocation works and prunes", t_revocation_and_pruning),
    ("deleted user's tokens die immediately", t_deleted_user_tokens_die),
    ("recreated username does not inherit sessions", t_recreated_username_does_not_resurrect_sessions),
    ("throttle locks out after max fails", t_throttle_locks_after_max_fails),
    ("throttle resets on success", t_throttle_success_resets),
    ("throttle window slides", t_throttle_window_slides),
    ("throttle keys are independent", t_throttle_keys_are_independent),
    ("throttle lockout expires", t_throttle_lockout_expires),
    ("throttle map is bounded (unauthenticated keys)", t_throttle_map_is_bounded_against_unauthenticated_growth),
    ("sweep never drops an active lockout", t_sweep_never_drops_an_active_lockout),
    ("IP policy is stricter than username policy", t_ip_policy_is_stricter_than_username_policy),
    ("attacker IP lockout does not lock the victim", t_login_helpers_lock_ip_without_locking_victim),
    ("successful login clears both keys", t_login_success_clears_both_keys),
    ("CSRF blocks cross-origin write", t_csrf_blocks_cross_origin_write),
    ("CSRF allows same-origin / non-browser", t_csrf_allows_same_origin_and_no_origin),
    ("cookie attributes differ public vs lab", t_cookie_attributes_public_vs_lab),
    ("unread body closes the connection", t_unread_body_closes_the_connection),
    ("every entry point resets per-request state", t_entry_points_actually_reset_state),
    ("body-consumed flag resets between requests", t_body_consumed_flag_resets_between_requests),
    ("X-Forwarded-For trusted only when configured", t_client_ip_trusts_xff_only_when_configured),
    ("connection slots are conserved on refusal", t_connection_slots_are_conserved),
]

print(f"gateway security unit tests ({len(checks)}):")
for name, fn in checks:
    check(name, fn)

if FAILS:
    print(f"FAIL: {len(FAILS)}/{len(checks)}")
    sys.exit(1)
print(f"PASS: {len(checks)}/{len(checks)}")
