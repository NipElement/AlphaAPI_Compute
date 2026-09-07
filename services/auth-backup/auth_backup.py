#!/usr/bin/env python3
"""Consistent, private SQLite backups. Never copy a live WAL database with cp.

The source may be mounted read-only; SQLite's backup API includes committed
WAL transactions. Replicate the destination off the cluster for node-loss DR.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import urllib.parse


def verify(path):
    path = Path(path).resolve(strict=True)
    uri = 'file:' + urllib.parse.quote(str(path)) + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('authentication backup integrity check failed')
        users = db.execute("SELECT count(*) FROM auth_state WHERE kind='users'").fetchone()[0]
        if not users:
            raise ValueError('authentication backup contains no accounts')
        for (raw,) in db.execute("SELECT value FROM auth_state WHERE kind='users'"):
            user = json.loads(raw)
            if user['role'] not in ('admin', 'user') or not user['hash'] or not user['ver']:
                raise ValueError('invalid account record in backup')
    return users


def backup(source, destination, keep=120):
    source = Path(source).resolve(strict=True)
    destination = Path(destination)
    if keep < 1:
        raise ValueError('keep must be positive')
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + f'-{time.time_ns():020d}'
    fd, tmp = tempfile.mkstemp(prefix='.auth-', suffix='.sqlite3', dir=destination)
    os.close(fd)
    try:
        uri = 'file:' + urllib.parse.quote(str(source)) + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True)) as src, closing(sqlite3.connect(tmp)) as dst:
            src.backup(dst)
        count = verify(tmp)
        with open(tmp, 'rb') as f:
            digest = hashlib.file_digest(f, 'sha256').hexdigest()
            os.fsync(f.fileno())
        final = destination / f'auth-{stamp}-{digest[:12]}.sqlite3'
        os.replace(tmp, final)
        directory = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        for old in sorted(destination.glob('auth-*.sqlite3'), reverse=True)[keep:]:
            old.unlink()
        return {'file': str(final), 'sha256': digest, 'accounts': count}
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', default='/source/auth.sqlite3')
    ap.add_argument('--destination', default='/backups')
    ap.add_argument('--keep', type=int, default=120)
    ap.add_argument('--verify', metavar='BACKUP')
    args = ap.parse_args()
    if args.verify:
        print(json.dumps({'verified': args.verify, 'accounts': verify(args.verify)}))
    else:
        print(json.dumps(backup(args.source, args.destination, args.keep)))


if __name__ == '__main__':
    main()
