import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from kiro_accounts import identity_json, parse_usage, probe_account, reset_cell
import test_accounts


class UsageParsingTest(unittest.TestCase):
    def test_real_kiro_shape(self):
        result = parse_usage({'success': True, 'data': {
            'billingCycleReset': '2026-11-01',
            'usageBreakdowns': [{'displayName': 'Credits', 'used': 222.74, 'limit': 2000}]}})
        self.assertEqual(result, [{'label': 'Credits', 'used': 222.74, 'limit': 2000,
                                  'remaining': 1777.26, 'reset': '2026-11-01'}])

    def test_multiple_quotas_trial_and_bonus(self):
        result = parse_usage({'usageBreakdowns': [
            {'used': 2100, 'limit': 2000, 'freeTrialInfo': {'used': 4, 'limit': 10}},
            {'displayName': 'Requests', 'used': 5, 'limit': 20}],
            'bonusCredits': [{'used': 2, 'limit': 50}]})
        self.assertEqual([row['remaining'] for row in result], [0, 6, 15, 48])
        self.assertEqual([row['label'] for row in result], ['Credits', 'Trial', 'Requests', 'Bonus'])

    def test_unknown_numbers_stay_unknown(self):
        self.assertEqual(parse_usage({'data': {'message': 'unavailable'}}), [])
        self.assertEqual(parse_usage({'used': True, 'limit': float('nan')}), [])
        self.assertIsNone(parse_usage({'creditsRemaining': 5})[0]['limit'])

    def test_remaining_only_and_epoch_reset(self):
        result = parse_usage({'result': {'data': {'creditsTotal': 100, 'creditsRemaining': 30}}})
        self.assertEqual(result[0]['used'], 70)
        self.assertEqual(reset_cell(1793491200000), '2026-11-01')

    def test_whoami_json_with_trailing_profile(self):
        self.assertEqual(identity_json('{"email":"you@example.com"}\nProfile: anything'),
                         {'email': 'you@example.com'})


class IsolatedProbeTest(unittest.TestCase):
    connect = test_accounts.AccountsTest.connect
    enroll = test_accounts.AccountsTest.enroll

    def setUp(self):
        test_accounts.AccountsTest.setUp(self)
        self.binary = str(Path(__file__).with_name('fake_kiro.py').resolve())

    def test_usage_does_not_change_live_database_or_selection(self):
        self.accounts.save('a')
        before = self.accounts.db.read_bytes()
        marker = (self.accounts.store / '.active.json').read_bytes()
        with patch.dict(os.environ, {'KIRO_API_KEY': 'must-not-be-used'}):
            result = self.accounts.check('a', self.binary, 5)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['email'], 'a@example.com')
        self.assertEqual(result['plan'], 'KIRO PRO+')
        self.assertEqual(result['limits'][0]['remaining'], 1777.26)
        self.assertEqual(self.accounts.db.read_bytes(), before)
        self.assertEqual((self.accounts.store / '.active.json').read_bytes(), marker)
        saved = self.accounts.read(self.accounts.path('a'))
        self.assertEqual(saved['email'], 'a@example.com')
        self.assertEqual(saved['auth'][0][1], 'account-a-refreshed')
        self.assertEqual(self.accounts.path('a').stat().st_mode & 0o777, 0o600)

    def test_save_captures_email(self):
        result = self.accounts.save('a', self.binary, 5)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(self.accounts.read(self.accounts.path('a'))['email'], 'a@example.com')

    def test_expired_account_does_not_hide_other_accounts_or_leak_error(self):
        self.enroll()
        data = self.accounts.read(self.accounts.path('a'))
        data['auth'][0][1] = 'expired'
        self.accounts.path('a').write_text(json.dumps(data))
        output = subprocess.run([sys.executable, 'kiro_accounts.py', '--db', str(self.accounts.db),
                                 '--store', str(self.accounts.store), '--kiro-binary', self.binary,
                                 '--timeout', '5', 'usage', '--json'], capture_output=True, text=True)
        self.assertEqual(output.returncode, 1)
        rows = json.loads(output.stdout)
        self.assertEqual(rows[0]['status'], 'login required')
        self.assertEqual(rows[1]['status'], 'ok')
        self.assertNotIn('SECRET', output.stdout + output.stderr)

    def test_timeout_is_bounded(self):
        self.accounts.save('a')
        data = self.accounts.read(self.accounts.path('a'))
        data['auth'][0][1] = 'hang'
        started = time.monotonic()
        result, _ = probe_account(data, self.binary, timeout=0.8)
        self.assertEqual(result['status'], 'timed out')
        self.assertLess(time.monotonic() - started, 4)

    def test_list_backfills_old_snapshot_and_then_uses_cache(self):
        self.accounts.save('a')
        command = [sys.executable, 'kiro_accounts.py', '--db', str(self.accounts.db),
                   '--store', str(self.accounts.store), '--kiro-binary', self.binary, 'list', '--json']
        first = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(first.stdout)[0]['email'], 'a@example.com')
        command[command.index(self.binary)] = '/nonexistent-kiro'
        cached = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(cached.stdout)[0]['status'], 'cached')

    def test_missing_binary_reports_failure_without_losing_snapshot(self):
        self.accounts.save('a')
        before = self.accounts.path('a').read_bytes()
        result = self.accounts.check('a', '/nonexistent-kiro', 1)
        self.assertEqual(result['status'], 'Kiro executable not found')
        self.assertEqual(self.accounts.path('a').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
