import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from kiro_accounts import AccountError, Accounts


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.accounts = Accounts(root / 'data.sqlite3', root / 'accounts')
        self.accounts.store.mkdir(mode=0o700)
        with self.connect() as conn:
            conn.executescript('''
                CREATE TABLE auth_kv(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE state(key TEXT PRIMARY KEY, value BLOB);
                CREATE TABLE conversations(key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO auth_kv VALUES ('token', 'account-a');
                INSERT INTO state VALUES ('auth.idc.region', 'region-a');
                INSERT INTO state VALUES ('settings', 'keep-me');
                INSERT INTO state VALUES ('api.codewhisperer.profile', X'0102');
                INSERT INTO conversations VALUES ('chat', 'keep-history');
            ''')

    @contextlib.contextmanager
    def connect(self):
        with contextlib.closing(sqlite3.connect(self.accounts.db)) as conn:
            with conn:
                yield conn

    def enroll(self):
        self.accounts.save('a')
        with self.connect() as conn:
            conn.execute("UPDATE auth_kv SET value='account-b'")
            conn.execute("DELETE FROM state WHERE key='auth.idc.region'")
            conn.execute("UPDATE state SET value=X'0304' WHERE key='api.codewhisperer.profile'")
        self.accounts.save('b')

    @patch('kiro_accounts.ensure_idle')
    def test_switch_and_restore_preserve_history_and_settings(self, idle):
        self.enroll()
        self.accounts.switch('a')
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT value FROM auth_kv').fetchone()[0], 'account-a')
            self.assertEqual(conn.execute("SELECT value FROM state WHERE key='api.codewhisperer.profile'").fetchone()[0], b'\x01\x02')
            self.assertEqual(conn.execute("SELECT value FROM state WHERE key='settings'").fetchone()[0], 'keep-me')
            self.assertEqual(conn.execute('SELECT value FROM conversations').fetchone()[0], 'keep-history')
        self.accounts.switch(restore=True)
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT value FROM auth_kv').fetchone()[0], 'account-b')
            self.assertIsNone(conn.execute("SELECT value FROM state WHERE key='auth.idc.region'").fetchone())
        self.assertEqual((self.accounts.store / 'a.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.accounts.store / '.before-switch.json').stat().st_mode & 0o777, 0o600)

    @patch('kiro_accounts.ensure_idle')
    def test_failed_write_rolls_back_credentials(self, idle):
        self.enroll()
        with self.connect() as conn:
            conn.execute("CREATE TRIGGER fail BEFORE INSERT ON state BEGIN SELECT RAISE(ABORT, 'failed'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.accounts.switch('a')
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT value FROM auth_kv').fetchone()[0], 'account-b')

    @patch('kiro_accounts.ensure_idle', side_effect=AccountError('busy'))
    def test_running_session_prevents_switch(self, idle):
        self.enroll()
        with self.assertRaises(AccountError):
            self.accounts.switch('a')
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT value FROM auth_kv').fetchone()[0], 'account-b')

    def test_path_traversal_and_extra_state_rejected(self):
        with self.assertRaises(AccountError):
            self.accounts.save('../escape')
        self.accounts.save('a')
        path = self.accounts.path('a')
        data = json.loads(path.read_text())
        data['state'].append(['settings', 'overwrite'])
        path.write_text(json.dumps(data))
        with self.assertRaises(AccountError):
            self.accounts.switch('a')

    def test_missing_database_is_not_created(self):
        self.accounts.db = self.accounts.db.parent / 'missing.sqlite3'
        with self.assertRaises(AccountError):
            self.accounts.save('a')
        self.assertFalse(self.accounts.db.exists())

    def test_rename_preserves_snapshot_and_updates_selected_alias(self):
        self.accounts.save('a')
        before = self.accounts.path('a').read_bytes()
        live = self.accounts.db.read_bytes()
        self.accounts.rename('a', 'vakyam')
        self.assertEqual(self.accounts.names(), ['vakyam'])
        self.assertEqual(self.accounts.path('vakyam').read_bytes(), before)
        self.assertEqual(self.accounts.active(), 'vakyam')
        self.assertEqual(self.accounts.db.read_bytes(), live)
        self.assertEqual(self.accounts.path('vakyam').stat().st_mode & 0o777, 0o600)

    def test_rename_rejects_collisions_missing_source_and_invalid_names(self):
        self.enroll()
        before = self.accounts.path('a').read_bytes()
        for old, new in [('a', 'b'), ('missing', 'new'), ('a', '../escape')]:
            with self.assertRaises(AccountError):
                self.accounts.rename(old, new)
        self.assertEqual(self.accounts.path('a').read_bytes(), before)
        self.assertEqual(self.accounts.active(), 'b')

    def test_rename_rolls_back_if_selection_update_fails(self):
        self.accounts.save('a')
        with patch('kiro_accounts.atomic_json', side_effect=OSError('failed')):
            with self.assertRaises(OSError):
                self.accounts.rename('a', 'new')
        self.assertEqual(self.accounts.names(), ['a'])
        self.assertEqual(self.accounts.active(), 'a')

    def test_delete_selected_alias_preserves_live_login_and_other_accounts(self):
        self.enroll()
        live = self.accounts.db.read_bytes()
        self.accounts.delete('b')
        self.assertEqual(self.accounts.names(), ['a'])
        self.assertIsNone(self.accounts.active())
        self.assertEqual(self.accounts.db.read_bytes(), live)

    def test_delete_other_alias_keeps_selection_and_rejects_invalid_names(self):
        self.enroll()
        self.accounts.delete('a')
        self.assertEqual(self.accounts.active(), 'b')
        for name in ('a', '../escape'):
            with self.assertRaises(AccountError):
                self.accounts.delete(name)

    def test_delete_restores_selection_when_unlink_fails(self):
        self.accounts.save('a')
        with patch.object(Path, 'unlink', side_effect=OSError('failed')):
            with self.assertRaises(OSError):
                self.accounts.delete('a')
        self.assertEqual(self.accounts.names(), ['a'])
        self.assertEqual(self.accounts.active(), 'a')


if __name__ == '__main__':
    unittest.main()
