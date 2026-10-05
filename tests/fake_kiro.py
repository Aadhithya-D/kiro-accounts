#!/usr/bin/env python3
"""Subprocess fixture for testing isolated CLI and ACP interaction."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

db = Path(os.environ['XDG_DATA_HOME']) / 'kiro-cli/data.sqlite3'
db.parent.mkdir(parents=True, exist_ok=True)
conn = sqlite3.connect(db)
conn.executescript('CREATE TABLE IF NOT EXISTS auth_kv(key TEXT PRIMARY KEY,value TEXT);'
                   'CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value BLOB);')
row = conn.execute('SELECT value FROM auth_kv LIMIT 1').fetchone()
token = row[0] if row else None
if sys.argv[1] == 'whoami':
    print(json.dumps({'email': 'a@example.com' if token.startswith('account-a') else 'b@example.com'}
                     if token else {'account': None}))
    print('Profile: trailing human-readable output')
    sys.exit(0 if token else 1)

for line in sys.stdin:
    message = json.loads(line)
    if 'method' not in message:
        continue
    method = message['method']
    if method == 'initialize':
        result = {'protocolVersion': 1, 'agentCapabilities': {}}
    elif method == 'session/new':
        if token == 'expired':
            print(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                              'error': {'code': -32000, 'message': 'Authentication expired SECRET'}}), flush=True)
            continue
        result = {'sessionId': 'test-session'}
    elif method == '_kiro.dev/commands/execute':
        if token == 'hang':
            time.sleep(60)
        # Exercise notification and server-request handling before the response.
        print(json.dumps({'jsonrpc': '2.0', 'method': 'session/update', 'params': {}}), flush=True)
        print(json.dumps({'jsonrpc': '2.0', 'id': 'permission', 'method': 'session/request_permission', 'params': {}}), flush=True)
        result = {'success': True, 'data': {
            'planName': 'KIRO PRO+', 'billingCycleReset': '2026-11-01',
            'usageBreakdowns': [{'displayName': 'Credits', 'used': 222.74, 'limit': 2000}],
            'bonusCredits': []}}
        conn.execute("UPDATE auth_kv SET value=?", (token + '-refreshed',))
        conn.commit()
    else:
        raise RuntimeError('Unexpected method')
    print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}), flush=True)
