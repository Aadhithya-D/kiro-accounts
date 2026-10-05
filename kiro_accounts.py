#!/usr/bin/env python3
"""Local account snapshots for Kiro CLI 2.x. Never prints credentials."""
import argparse
import base64
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

STATE_KEYS = ('api.codewhisperer.profile', 'auth.idc.region', 'auth.idc.start-url')


class AccountError(Exception):
    pass


def atomic_json(path, data):
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.write-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def encode(value):
    if isinstance(value, bytes):
        return {'bytes': base64.b64encode(value).decode('ascii')}
    return value


def decode(value):
    if isinstance(value, dict):
        if set(value) != {'bytes'}:
            raise AccountError('Invalid snapshot value.')
        return base64.b64decode(value['bytes'], validate=True)
    if value is not None and not isinstance(value, str):
        raise AccountError('Invalid snapshot value.')
    return value


class Accounts:
    def __init__(self, db, store):
        self.db = Path(db)
        self.store = Path(store)

    @contextlib.contextmanager
    def lock(self):
        self.store.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.store, 0o700)
        fd = os.open(self.store / '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'w') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def path(self, name):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name):
            raise AccountError('Use a name of 1–64 letters, digits, underscores or hyphens.')
        return self.store / (name + '.json')

    def names(self):
        return sorted(p.stem for p in self.store.glob('*.json') if not p.name.startswith('.'))

    def connect(self):
        if not self.db.is_file():
            raise AccountError('Kiro database not found. Run kiro-cli login first.')
        conn = sqlite3.connect(f'{self.db.resolve().as_uri()}?mode=rw', uri=True, timeout=5)
        schemas = {name: [(row[1], row[2].upper()) for row in conn.execute(f'PRAGMA table_info({name})')]
                   for name in ('auth_kv', 'state')}
        if schemas != {'auth_kv': [('key', 'TEXT'), ('value', 'TEXT')],
                       'state': [('key', 'TEXT'), ('value', 'BLOB')]}:
            conn.close()
            raise AccountError('Unsupported Kiro database schema; no changes made.')
        return conn

    def snapshot(self, conn):
        return {'version': 1,
                'auth': [[key, encode(value)] for key, value in conn.execute('SELECT key,value FROM auth_kv ORDER BY key')],
                'state': [[key, encode(value)] for key, value in conn.execute(
                    'SELECT key,value FROM state WHERE key IN (?,?,?) ORDER BY key', STATE_KEYS)]}

    def read(self, path):
        if not path.is_file():
            raise AccountError('Account or backup not found.')
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('auth'), list) or not isinstance(data.get('state'), list):
            raise AccountError('Invalid account snapshot.')
        for group in ('auth', 'state'):
            seen = set()
            for row in data[group]:
                if not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str) or row[0] in seen:
                    raise AccountError('Invalid snapshot rows.')
                seen.add(row[0])
                decode(row[1])
                if group == 'state' and row[0] not in STATE_KEYS:
                    raise AccountError('Snapshot contains unsupported state keys.')
        if not data['auth']:
            raise AccountError('Snapshot has no credentials.')
        return data

    def save(self, name):
        path = self.path(name)
        with contextlib.closing(self.connect()) as conn:
            conn.execute('BEGIN')
            data = self.snapshot(conn)
        if not data['auth']:
            raise AccountError('No browser login to save. Run kiro-cli login first.')
        atomic_json(path, data)
        atomic_json(self.store / '.active.json', {'name': name})

    def active(self):
        path = self.store / '.active.json'
        return json.loads(path.read_text()).get('name') if path.exists() else None

    def switch(self, name=None, restore=False):
        data = self.read(self.store / '.before-switch.json' if restore else self.path(name))
        ensure_idle()
        with contextlib.closing(self.connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            ensure_idle()
            previous = self.snapshot(conn)
            # Save a recoverable copy before touching the live credentials.
            atomic_json(self.store / '.before-switch.json', previous)
            try:
                conn.execute('DELETE FROM auth_kv')
                conn.executemany('INSERT INTO auth_kv(key,value) VALUES (?,?)',
                                 [(k, decode(v)) for k, v in data['auth']])
                conn.execute('DELETE FROM state WHERE key IN (?,?,?)', STATE_KEYS)
                conn.executemany('INSERT INTO state(key,value) VALUES (?,?)',
                                 [(k, decode(v)) for k, v in data['state']])
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        atomic_json(self.store / '.active.json', {'name': None if restore else name})


def ensure_idle():
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            executable = (proc / 'exe').resolve().name
            if executable in ('kiro-cli', 'kiro-cli-chat', 'kiro-cli-chat-real'):
                raise AccountError('Close running Kiro chat/ACP sessions first, including Kiro sessions in T3 Code.')
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(Path.home() / '.local/share/kiro-cli/data.sqlite3'))
    parser.add_argument('--store', default=str(Path.home() / '.local/share/kiro-accounts'))
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('save', 'use'):
        sub = commands.add_parser(command)
        sub.add_argument('name')
    for command in ('list', 'next', 'restore'):
        commands.add_parser(command)
    args = parser.parse_args()
    accounts = Accounts(args.db, args.store)
    try:
        with accounts.lock():
            if args.command == 'save':
                accounts.save(args.name)
                print(f'Saved login as {args.name}.')
            elif args.command == 'list':
                active = accounts.active()
                for name in accounts.names():
                    print(f'{name}' + (' (last selected)' if name == active else ''))
                if not accounts.names():
                    print('No saved accounts. Run: kiro-accounts save NAME')
            elif args.command == 'restore':
                accounts.switch(restore=True)
                print('Restored credentials from before the last switch.')
            else:
                name = getattr(args, 'name', None)
                if args.command == 'next':
                    names = accounts.names()
                    if len(names) < 2:
                        raise AccountError('Save at least two accounts before using next.')
                    active = accounts.active()
                    name = names[(names.index(active) + 1) % len(names)] if active in names else names[0]
                accounts.switch(name)
                print(f'Selected {name}. Check with kiro-cli whoami, then reopen your Kiro session.')
        return 0
    except (AccountError, sqlite3.Error, OSError, ValueError, TypeError, KeyError) as error:
        # Avoid propagating exceptions containing credential values.
        print(str(error) if isinstance(error, AccountError) else 'Account operation failed; check file permissions and snapshot format.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
