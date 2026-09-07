#!/usr/bin/env python3
"""Tenant access: SSH/scp ProxyCommand, or a loopback tunnel to a private service.

Requires the platform's published host keys in a known_hosts file. Never disables
host verification. No Kubernetes credential or third-party Python package needed.
"""
import argparse
import os
import re
import socket
import socketserver
import subprocess
import sys
import threading


def ssh_args(args):
    return ['ssh', '-T', '-p', str(args.port), '-i', args.key,
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
            '-o', 'UserKnownHostsFile=' + args.known_hosts, 'dev@' + args.host]


def serve(args):
    slots = threading.BoundedSemaphore(16)
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            if not slots.acquire(blocking=False):
                return
            channel = process = None
            try:
                channel, child = socket.socketpair()
                process = subprocess.Popen(ssh_args(args) + ['service:' + args.name],
                                           stdin=child, stdout=child, stderr=sys.stderr)
                child.close()
                channel.settimeout(900)
                self.request.settimeout(900)
                def copy(source, destination):
                    try:
                        while data := source.recv(65536):
                            destination.sendall(data)
                    except OSError:
                        pass
                    finally:
                        try:
                            destination.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                forward = threading.Thread(target=copy, args=(self.request, channel), daemon=True)
                forward.start()
                copy(channel, self.request)
                channel.shutdown(socket.SHUT_RDWR)
                self.request.shutdown(socket.SHUT_RDWR)
                forward.join(timeout=2)
            except OSError:
                pass
            finally:
                if channel:
                    channel.close()
                if process:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait()
                slots.release()
    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True
    with Server(('127.0.0.1', args.local_port), Handler) as server:
        print(f'Private service available at http://127.0.0.1:{server.server_address[1]}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('mode', choices=('proxy', 'service'))
    ap.add_argument('name', help='development machine or service name, without namespace or address')
    ap.add_argument('--host', required=True, help='platform SSH access hostname')
    ap.add_argument('--port', type=int, default=2222)
    ap.add_argument('--key', required=True, help='private key registered for bastion access')
    ap.add_argument('--known-hosts', required=True, help='file containing verified bastion and machine host keys')
    ap.add_argument('--local-port', type=int, default=8080)
    args = ap.parse_args()
    if (not re.fullmatch(r'[a-z][a-z0-9-]{0,56}[a-z0-9]|[a-z]', args.name)
            or not re.fullmatch(r'[A-Za-z0-9.:-]+', args.host)
            or not 1 <= args.port <= 65535 or not 0 <= args.local_port <= 65535):
        ap.error('invalid target name, host, or port')
    if args.mode == 'proxy':
        os.execvp('ssh', ssh_args(args) + [args.name])
    serve(args)


if __name__ == '__main__':
    main()
