#!/usr/bin/env python3
"""Forced SSH command: one registered namespace, one machine, fixed SSH port.

No shell, arbitrary address, client-selected namespace, or TCP forwarding is
available. The inner SSH connection still authenticates to the tenant machine.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import socket
import sys
import time

RUNTIME = Path('/run/access-slots')
MAX_TUNNELS = 32
BUFFER_LIMIT = 256 * 1024
IDLE_SECONDS = 15 * 60
LIFETIME_SECONDS = 12 * 3600


def destination(tenant, command):
    if not isinstance(tenant, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', tenant):
        raise ValueError('invalid configured tenant')
    if not isinstance(command, str):
        raise ValueError('supply a machine name or service:NAME')
    service = command.startswith('service:')
    name = command[len('service:'):] if service else command
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,56}[a-z0-9]|[a-z]', name):
        raise ValueError('supply only a machine name or service:NAME')
    if service:
        return f'{name}.{tenant}.svc.cluster.local', 80
    return f'{name}-ssh.{tenant}.svc.cluster.local', 22


def acquire_slot(directory=RUNTIME):
    for index in range(MAX_TUNNELS):
        fd = os.open(directory / f'slot-{index}', os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            os.close(fd)
    raise RuntimeError('access gateway is busy; try again shortly')


def audit(event, **fields):
    fields.update(event=event, component='ssh-bastion', timestamp=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as log:
        log.settimeout(1)
        log.sendto(json.dumps(fields, separators=(',', ':')).encode(), str(RUNTIME/'audit.sock'))


def relay(remote, input_fd=0, output_fd=1, idle=IDLE_SECONDS, lifetime=LIFETIME_SECONDS):
    """Bounded bidirectional relay with backpressure and half-close support."""
    to_remote, to_client = bytearray(), bytearray()
    input_open = remote_open = True
    write_closed = False
    started = last_activity = time.monotonic()
    old_input, old_output = os.get_blocking(input_fd), os.get_blocking(output_fd)
    os.set_blocking(input_fd, False)
    os.set_blocking(output_fd, False)
    remote.setblocking(False)
    try:
        while remote_open or to_client:
            now = time.monotonic()
            remaining = min(idle - (now - last_activity), lifetime - (now - started))
            if remaining <= 0:
                raise TimeoutError('SSH tunnel idle or lifetime limit reached')
            if not input_open and not to_remote and not write_closed:
                remote.shutdown(socket.SHUT_WR)
                write_closed = True
            with selectors.DefaultSelector() as selector:
                if input_open and remote_open and len(to_remote) < BUFFER_LIMIT:
                    selector.register(input_fd, selectors.EVENT_READ, 'input')
                if to_client:
                    selector.register(output_fd, selectors.EVENT_WRITE, 'output')
                mask = 0
                if remote_open and len(to_client) < BUFFER_LIMIT:
                    mask |= selectors.EVENT_READ
                if remote_open and to_remote:
                    mask |= selectors.EVENT_WRITE
                if mask:
                    selector.register(remote, mask, 'remote')
                for key, events in selector.select(min(remaining, 5)):
                    try:
                        if key.data == 'input':
                            data = os.read(input_fd, min(65536, BUFFER_LIMIT - len(to_remote)))
                            if data:
                                to_remote.extend(data)
                            else:
                                input_open = False
                        elif key.data == 'output':
                            count = os.write(output_fd, to_client)
                            del to_client[:count]
                        else:
                            if events & selectors.EVENT_READ:
                                data = remote.recv(min(65536, BUFFER_LIMIT - len(to_client)))
                                if data:
                                    to_client.extend(data)
                                else:
                                    remote_open = False
                            if events & selectors.EVENT_WRITE and remote_open:
                                count = remote.send(to_remote)
                                del to_remote[:count]
                        last_activity = time.monotonic()
                    except BlockingIOError:
                        pass
    finally:
        os.set_blocking(input_fd, old_input)
        os.set_blocking(output_fd, old_output)


def main():
    slot = None
    tenant = sys.argv[1] if len(sys.argv) == 2 else ''
    machine = os.environ.get('SSH_ORIGINAL_COMMAND', '')
    client = os.environ.get('SSH_CONNECTION', '').split(' ', 1)[0]
    try:
        host, port = destination(tenant, machine)
        slot = acquire_slot()
        # Audit must be accepted by the container logger before opening access.
        audit('connect', tenant=tenant, machine=machine, client=client)
        with socket.create_connection((host, port), timeout=10) as remote:
            relay(remote)
        audit('disconnect', tenant=tenant, machine=machine, client=client)
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f'Access refused: {exc}', file=sys.stderr)
        try:
            audit('refused', tenant=tenant, machine=machine[:80], client=client, error=type(exc).__name__)
        except OSError:
            pass
        return 1
    finally:
        if slot is not None:
            os.close(slot)


if __name__ == '__main__':
    sys.exit(main())
