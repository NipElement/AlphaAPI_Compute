#!/usr/bin/env python3
"""Run unprivileged sshd and forward forced-command audit events to stdout."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys

RUNTIME = Path('/run/access-slots')


def main():
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    hostkey = Path('/keys/ssh_host_ed25519_key')
    if not hostkey.exists():
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(hostkey)], check=True)
    hostkey.chmod(0o600)
    config = '/etc/arise/access/sshd_config'
    subprocess.run(['/usr/sbin/sshd', '-t', '-f', config], check=True)
    address = RUNTIME/'audit.sock'
    address.unlink(missing_ok=True)
    ready = RUNTIME/'ready'
    ready.unlink(missing_ok=True)
    stop = False
    def shutdown(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as events:
        events.bind(str(address))
        address.chmod(0o600)
        events.settimeout(1)
        daemon = subprocess.Popen(['/usr/sbin/sshd', '-D', '-e', '-f', config], start_new_session=True)
        try:
            while not stop and daemon.poll() is None:
                if not ready.exists():
                    try:
                        with socket.create_connection(('127.0.0.1', 2222), timeout=.2):
                            ready.touch(mode=0o600)
                    except OSError:
                        pass
                try:
                    data = events.recv(4096)
                    print(json.dumps(json.loads(data)), flush=True)
                except socket.timeout:
                    pass
                except (ValueError, UnicodeError):
                    print(json.dumps({'component': 'ssh-bastion', 'event': 'invalid-audit-event'}), flush=True)
            return 0 if stop else 1
        finally:
            ready.unlink(missing_ok=True)
            try:
                os.killpg(daemon.pid, signal.SIGTERM)
                daemon.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(daemon.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                daemon.wait()


if __name__ == '__main__':
    sys.exit(main())
