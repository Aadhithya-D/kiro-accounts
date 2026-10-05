#!/usr/bin/env python3
"""Local account snapshots for Kiro CLI 2.x. Never prints credentials."""
import argparse
import base64
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import queue
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

STATE_KEYS = ('api.codewhisperer.profile', 'auth.idc.region', 'auth.idc.start-url')


class AccountError(Exception):
    pass


class ProbeError(AccountError):
    pass


def identity_json(output):
    # Kiro 2.7 appends a human-readable profile after its JSON object.
    data, _ = json.JSONDecoder().raw_decode(output.lstrip())
    return data if isinstance(data, dict) else {}


class AcpProbe:
    def __init__(self, binary, env, cwd, deadline):
        self.deadline = deadline
        self.messages = queue.Queue()
        self.sequence = 0
        self.proc = subprocess.Popen([binary, 'acp'], env=env, cwd=cwd,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True,
                                     start_new_session=True)
        self.reader = threading.Thread(target=self.read_loop, daemon=True)
        self.reader.start()

    def read_loop(self):
        try:
            for line in self.proc.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            self.messages.put(None)

    def request(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeError('timed out')
            try:
                message = self.messages.get(timeout=remaining)
            except queue.Empty:
                raise ProbeError('timed out') from None
            if message is None:
                raise ProbeError('Kiro process exited')
            if not isinstance(message, dict):
                continue
            if 'method' in message:
                if 'id' in message:
                    # A quota probe never approves tools or interactive sign-in.
                    if message['method'] == 'session/request_permission':
                        self.send({'jsonrpc': '2.0', 'id': message['id'],
                                   'result': {'outcome': {'outcome': 'cancelled'}}})
                    else:
                        self.send({'jsonrpc': '2.0', 'id': message['id'],
                                   'error': {'code': -32601, 'message': 'Unsupported by usage probe'}})
                continue
            if message.get('id') != request_id:
                continue
            if 'error' in message:
                error = message['error']
                text = json.dumps(error).lower()
                if any(word in text for word in ('unauthorized', 'expired', 'authentication', 'login', 'sign in', '403', '401')):
                    raise ProbeError('login required')
                if isinstance(error, dict) and error.get('code') == -32601:
                    raise ProbeError('usage unsupported by this Kiro version')
                raise ProbeError('Kiro usage request failed')
            return message.get('result')

    def send(self, message):
        try:
            self.proc.stdin.write(json.dumps(message) + '\n')
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise ProbeError('Kiro process exited') from None

    def close(self):
        # Kill the isolated process group, including any children Kiro spawned.
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=2)
        self.reader.join(timeout=2)
        self.proc.stdin.close()
        self.proc.stdout.close()


@contextlib.contextmanager
def isolated_login(data, binary, timeout):
    with tempfile.TemporaryDirectory(prefix='kiro-account-probe-') as temp:
        root = Path(temp)
        env = os.environ.copy()
        for key in ('KIRO_API_KEY', 'KIRO_ACP_RECORD_PATH', 'KIRO_AGENT_PATH', 'KIRO_DATA_DIR',
                    'KIRO_LOG_STDOUT', 'Q_LOG_STDOUT', 'KIRO_CHAT_LOG_FILE'):
            env.pop(key, None)
        env.update(XDG_DATA_HOME=str(root / 'data'), XDG_CONFIG_HOME=str(root / 'config'),
                   XDG_CACHE_HOME=str(root / 'cache'), KIRO_HOME=str(root / 'kiro'),
                   KIRO_DISABLE_TELEMETRY='1', Q_DISABLE_TELEMETRY='1')
        # Let Kiro create its own migrated schema in the isolated directory.
        initialized = subprocess.run([binary, 'whoami', '--format', 'json'], env=env,
                                     cwd=root, capture_output=True, text=True, timeout=timeout)
        db = root / 'data/kiro-cli/data.sqlite3'
        initial_identity = identity_json(initialized.stdout)
        if not db.is_file() or 'account' not in initial_identity or initial_identity['account'] is not None:
            raise ProbeError('Kiro data isolation unavailable')
        with contextlib.closing(sqlite3.connect(db)) as conn:
            with conn:
                conn.execute('DELETE FROM auth_kv')
                conn.executemany('INSERT INTO auth_kv(key,value) VALUES (?,?)',
                                 [(k, decode(v)) for k, v in data['auth']])
                conn.execute('DELETE FROM state WHERE key IN (?,?,?)', STATE_KEYS)
                conn.executemany('INSERT INTO state(key,value) VALUES (?,?)',
                                 [(k, decode(v)) for k, v in data['state']])
        yield env, root, db


def probe_account(data, binary='kiro-cli', timeout=45, usage=True):
    deadline = time.monotonic() + timeout
    result = {'email': data.get('email'), 'status': 'ok', 'plan': None, 'limits': [],
              'checked_at': datetime.now(timezone.utc).isoformat()}
    refreshed = None
    try:
        binary = shutil.which(binary)
        if not binary:
            raise ProbeError('Kiro executable not found')
        binary = str(Path(binary).resolve())
        with isolated_login(data, binary, max(0.1, deadline - time.monotonic())) as (env, root, db):
            identity = subprocess.run([binary, 'whoami', '--format', 'json'], env=env,
                                      cwd=root, capture_output=True, text=True,
                                      timeout=max(0.1, deadline - time.monotonic()))
            info = identity_json(identity.stdout)
            if identity.returncode or info.get('account', 'present') is None:
                raise ProbeError('login required')
            result['email'] = info.get('email') or result['email']
            if usage:
                acp = AcpProbe(binary, env, root, deadline)
                try:
                    acp.request('initialize', {'protocolVersion': 1, 'clientCapabilities': {},
                                              'clientInfo': {'name': 'kiro-accounts', 'version': '0.2.0'}})
                    session = acp.request('session/new', {'cwd': str(root), 'mcpServers': []})
                    payload = acp.request('_kiro.dev/commands/execute', {
                        'sessionId': session['sessionId'], 'command': {'command': 'usage', 'args': {}}})
                    root_payload = usage_root(payload)
                    result['plan'] = root_payload.get('planName')
                    result['limits'] = parse_usage(payload)
                    if not result['limits']:
                        result['status'] = 'usage unavailable'
                except ProbeError as error:
                    result['status'] = str(error)
                finally:
                    acp.close()
            with contextlib.closing(sqlite3.connect(db)) as conn:
                refreshed = Accounts(db, root).snapshot(conn)
    except ProbeError as error:
        result['status'] = str(error)
    except subprocess.TimeoutExpired:
        result['status'] = 'timed out'
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        result['status'] = 'account probe failed'
    return result, refreshed


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def first_number(row, *keys):
    return next((number(row[key]) for key in keys if number(row.get(key)) is not None), None)


def usage_root(payload):
    while isinstance(payload, dict):
        if isinstance(payload.get('data'), dict):
            payload = payload['data']
        elif isinstance(payload.get('result'), dict):
            payload = payload['result']
        else:
            return payload
    return {}


def parse_usage(payload):
    payload = usage_root(payload)
    rows = payload.get('usageBreakdowns', payload.get('usageBreakdownList', payload.get('breakdowns')))
    if not isinstance(rows, list):
        rows = [payload]
    limits = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        used = first_number(row, 'currentUsage', 'used', 'creditsUsed', 'credits_used')
        total = first_number(row, 'usageLimit', 'limit', 'creditsTotal', 'credits_total', 'credits_limit')
        remaining = first_number(row, 'remaining', 'creditsRemaining')
        if used is None and total is not None and remaining is not None:
            used = total - remaining
        if remaining is None and total is not None and used is not None:
            remaining = max(0, total - used)
        if all(value is None for value in (used, total, remaining)):
            continue
        limits.append({'label': row.get('displayNamePlural') or row.get('displayName') or row.get('type') or 'Credits',
                       'used': used, 'limit': total, 'remaining': remaining,
                       'reset': row.get('resetDate') or row.get('nextDateReset') or row.get('resetsAt') or row.get('resetAt') or row.get('resets') or payload.get('nextDateReset') or payload.get('billingCycleReset')})
        for key, label in (('freeTrialInfo', 'Trial'),):
            extra = row.get(key)
            if isinstance(extra, dict):
                extra = dict(extra, displayName=label)
                limits.extend(parse_usage(extra))
        for bonus in row.get('bonuses') or []:
            if isinstance(bonus, dict):
                limits.extend(parse_usage(dict(bonus, displayName=bonus.get('displayName') or 'Bonus')))
    for bonus in payload.get('bonusCredits') or []:
        if isinstance(bonus, dict):
            limits.extend(parse_usage(dict(bonus, displayName=bonus.get('displayName') or 'Bonus')))
    return limits


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
        if data.get('email') is not None and not isinstance(data['email'], str):
            raise AccountError('Invalid account email.')
        return data

    def save(self, name, binary=None, timeout=45):
        path = self.path(name)
        with contextlib.closing(self.connect()) as conn:
            conn.execute('BEGIN')
            data = self.snapshot(conn)
        if not data['auth']:
            raise AccountError('No browser login to save. Run kiro-cli login first.')
        identity = None
        if binary:
            identity, refreshed = probe_account(data, binary, timeout, usage=False)
            if refreshed:
                data = refreshed
            if identity.get('email'):
                data['email'] = identity['email']
        atomic_json(path, data)
        atomic_json(self.store / '.active.json', {'name': name})
        return identity

    def check(self, name, binary, timeout, usage=True):
        path = self.path(name)
        data = self.read(path)
        result, refreshed = probe_account(data, binary, timeout, usage)
        if refreshed:
            data.update(refreshed)
        if result['email']:
            data['email'] = result['email']
        if refreshed or result['email']:
            atomic_json(path, data)
        return dict(result, account=name)

    def active(self):
        path = self.store / '.active.json'
        return json.loads(path.read_text()).get('name') if path.exists() else None

    def rename(self, name, new_name):
        source, destination = self.path(name), self.path(new_name)
        if not source.is_file():
            raise AccountError('Saved account not found.')
        if os.path.lexists(destination):
            raise AccountError('An account with the new name already exists.')
        selected = self.active() == name
        source.rename(destination)
        try:
            if selected:
                atomic_json(self.store / '.active.json', {'name': new_name})
        except BaseException:
            destination.rename(source)
            raise

    def delete(self, name):
        path = self.path(name)
        if not path.is_file():
            raise AccountError('Saved account not found.')
        selected = self.active() == name
        if selected:
            atomic_json(self.store / '.active.json', {'name': None})
        try:
            path.unlink()
        except BaseException:
            if selected:
                atomic_json(self.store / '.active.json', {'name': name})
            raise

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


def safe_cell(value):
    if value is None:
        return '—'
    if isinstance(value, (int, float)):
        return f'{value:,.2f}'.rstrip('0').rstrip('.')
    return ''.join(char for char in str(value) if char.isprintable())[:120] or '—'


def reset_cell(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000 if value > 1e12 else value,
                                          timezone.utc).strftime('%Y-%m-%d')
        except (ValueError, OverflowError, OSError):
            return '—'
    return safe_cell(value)


def print_table(headers, rows):
    cells = [[safe_cell(cell) for cell in row] for row in rows]
    widths = [max(len(header), *(len(row[i]) for row in cells)) for i, header in enumerate(headers)]
    print('  '.join(header.ljust(width) for header, width in zip(headers, widths)))
    for row in cells:
        print('  '.join(cell.ljust(width) for cell, width in zip(row, widths)))


def account_result(accounts, name, binary, timeout, usage):
    try:
        return accounts.check(name, binary, timeout, usage)
    except (AccountError, OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return {'account': name, 'email': None, 'status': 'invalid account snapshot', 'limits': []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(Path.home() / '.local/share/kiro-cli/data.sqlite3'))
    parser.add_argument('--store', default=str(Path.home() / '.local/share/kiro-accounts'))
    parser.add_argument('--kiro-binary', default='kiro-cli', help='Kiro executable used for isolated queries')
    parser.add_argument('--timeout', type=float, default=45, help='Maximum seconds per account query (default: 45)')
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('save', 'use', 'delete'):
        sub = commands.add_parser(command)
        sub.add_argument('name')
    rename = commands.add_parser('rename', help='Rename a saved account alias')
    rename.add_argument('name')
    rename.add_argument('new_name')
    for command in ('next', 'restore'):
        commands.add_parser(command)
    for command in ('list', 'usage'):
        sub = commands.add_parser(command)
        sub.add_argument('--json', action='store_true', help='Output structured JSON')
        if command == 'list':
            sub.add_argument('--refresh', action='store_true', help='Refresh all saved email labels')
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--timeout must be a positive finite number')
    accounts = Accounts(args.db, args.store)
    try:
        with accounts.lock():
            if args.command == 'rename':
                accounts.rename(args.name, args.new_name)
                print(f'Renamed {args.name} to {args.new_name}.')
            elif args.command == 'delete':
                accounts.delete(args.name)
                print(f'Deleted saved login {args.name}. Active Kiro login unchanged.')
            elif args.command == 'save':
                identity = accounts.save(args.name, args.kiro_binary, args.timeout)
                email = f" ({safe_cell(identity['email'])})" if identity and identity.get('email') else ''
                print(f'Saved login as {args.name}{email}.')
                if identity and identity['status'] != 'ok':
                    print(f"Email lookup: {identity['status']}; credentials were saved.")
            elif args.command in ('list', 'usage'):
                active = accounts.active()
                results = []
                for name in accounts.names():
                    try:
                        data = accounts.read(accounts.path(name))
                        if args.command == 'usage' or args.refresh or not data.get('email'):
                            result = account_result(accounts, name, args.kiro_binary, args.timeout, args.command == 'usage')
                        else:
                            result = {'account': name, 'email': data['email'], 'status': 'cached'}
                    except (AccountError, OSError, ValueError, TypeError, KeyError):
                        result = {'account': name, 'email': None, 'status': 'invalid account snapshot', 'limits': []}
                    result['last_selected'] = name == active
                    results.append(result)
                if args.json:
                    print(json.dumps(results, indent=2))
                elif not results:
                    print('No saved accounts. Run: kiro-accounts save NAME')
                elif args.command == 'list':
                    print_table(('Account', 'Email', 'Selection', 'Status'),
                                [(r['account'], r['email'], 'last selected' if r['last_selected'] else '', r['status']) for r in results])
                else:
                    rows = []
                    for result in results:
                        for limit in result['limits'] or [{}]:
                            rows.append((result['account'], result['email'], result.get('plan'), limit.get('label'),
                                         limit.get('used'), limit.get('limit'), limit.get('remaining'),
                                         reset_cell(limit.get('reset')), result['status']))
                    print_table(('Account', 'Email', 'Plan', 'Quota', 'Used', 'Limit', 'Remaining', 'Reset', 'Status'), rows)
                return int(any(r['status'] not in ('ok', 'cached') for r in results))
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
