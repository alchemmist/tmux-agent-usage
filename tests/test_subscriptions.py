import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('subscriptions', ROOT / 'scripts/subscriptions.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ParserTests(unittest.TestCase):
    def test_chatgpt_window_duration_not_position(self):
        values = MODULE.parse_chatgpt({'rate_limit': {'primary_window': {
            'used_percent': 13, 'limit_window_seconds': 604800, 'reset_at': 2000000000}}})
        self.assertEqual(values, {'WEEK_PCT': 13, 'WEEK_RESET': 2000000000})

    def test_chatgpt_ignores_extra_model_limits(self):
        values = MODULE.parse_chatgpt({'rate_limit': {'primary_window': {
            'used_percent': 7, 'limit_window_seconds': 18000}},
            'additional_rate_limits': [{'used_percent': 99}]})
        self.assertEqual(values['CURRENT_PCT'], 7)
        self.assertNotIn('WEEK_PCT', values)

    def test_cursor_fraction_is_already_percent(self):
        values = MODULE.parse_cursor({'individualUsage': {'plan': {'totalPercentUsed': 0.36}},
                                     'billingCycleEnd': '2026-12-01T00:00:00.000Z'})
        self.assertEqual(values['PCT'], 0.36)
        self.assertEqual(values['RESET'], 1796083200)

    def test_cursor_fallback_and_no_fabricated_zero(self):
        self.assertEqual(MODULE.parse_cursor({'individualUsage': {'plan': {'used': 40, 'limit': 200}}})['PCT'], 20)
        for data in ({}, {'individualUsage': {'plan': {'totalPercentUsed': True}}},
                     {'individualUsage': {'plan': {'totalPercentUsed': float('nan')}}}):
            with self.assertRaises(MODULE.UsageError):
                MODULE.parse_cursor(data)

    def test_failure_preserves_last_success_and_throttles_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            cache = home / 'cache'
            cache.mkdir()
            with patch.object(MODULE, 'fetch_cursor', return_value={'PCT': 42, 'RESET': 2000000000}):
                MODULE.refresh('cursor', home, cache, 60, False, '')
            original = MODULE.read_cache(cache / 'cursor')
            with patch.object(MODULE, 'fetch_cursor', side_effect=MODULE.UsageError('network')) as fetch:
                MODULE.refresh('cursor', home, cache, 60, False, '')
                fetch.assert_not_called()
                MODULE.refresh('cursor', home, cache, 60, True, '')
            current = MODULE.read_cache(cache / 'cursor')
            self.assertEqual(current['PCT'], '42')
            self.assertEqual(current['UPDATED_AT'], original['UPDATED_AT'])
            self.assertEqual(current['STATUS'], 'network')

    def test_expired_auth_refresh_uses_official_client_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            binary = home / '.local/bin/antex'
            binary.parent.mkdir(parents=True)
            binary.write_text(f'#!{sys.executable}\nimport sys,json,time\n'
                              'for line in sys.stdin:\n'
                              ' message=json.loads(line)\n'
                              ' if message.get("id")==2:\n'
                              '  assert message["method"]=="account/read"\n'
                              '  assert message["params"]["refreshToken"] is True\n'
                              '  print(json.dumps({"id":2,"result":{}}),flush=True)\n'
                              '  time.sleep(30)\n')
            binary.chmod(0o755)
            with patch.object(MODULE.shutil, 'which', return_value=None):
                MODULE.refresh_chatgpt_auth(home)

    def test_credentials_not_in_command_arguments(self):
        result = subprocess.CompletedProcess([], 0, '{}\n200', '')
        with patch.object(MODULE.subprocess, 'run', return_value=result) as run:
            MODULE.request('https://cursor.com/api/usage-summary', ['Cookie: secret'])
        self.assertNotIn('secret', str(run.call_args.args))
        self.assertIn('secret', run.call_args.kwargs['input'])


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.socket = str(self.home / 'socket')
        self.tmux('new-session', '-d')
        self.addCleanup(self.tmux, 'kill-server')
        (self.home / 'tmux').write_text(f'#!/bin/sh\nexec {shutil.which("tmux")} -S {self.socket} "$@"\n')
        (self.home / 'tmux').chmod(0o755)
        scripts = self.home / 'scripts'
        scripts.mkdir()
        for name in ('segment.sh', 'helpers.sh', 'subscriptions.sh'):
            shutil.copy(ROOT / 'scripts' / name, scripts)
        (scripts / 'subscriptions.py').write_text('')
        self.cache = self.home / '.cache/claude-usage/subscriptions'
        self.cache.mkdir(parents=True)
        self.tmux('set', '-g', '@agent_usage_providers', 'chatgpt,cursor')
        self.tmux('set', '-g', '@agent_usage_chatgpt_label', 'chatgpt')
        self.tmux('set', '-g', '@claude_usage_style', 'gauge')
        self.env = dict(os.environ, HOME=str(self.home), PATH=f'{self.home}:{os.environ["PATH"]}')

    def tmux(self, *args):
        return subprocess.check_output(['tmux', '-S', self.socket, '-f', '/dev/null', *args], text=True)

    def render(self):
        text = subprocess.check_output(['bash', str(self.home / 'scripts/segment.sh')], env=self.env, text=True)
        return re.sub(r'#\[.*?\]', '', text)

    def seed(self, reset=2000000000, status='ok', age=0):
        now = int(time.time()) - age
        (self.cache / 'chatgpt').write_text(f'WEEK_PCT=13\nWEEK_RESET={reset}\nSTATUS={status}\nUPDATED_AT={now}\n')
        (self.cache / 'cursor').write_text(f'PCT=0\nRESET={reset}\nSTATUS={status}\nUPDATED_AT={now}\n')

    def test_two_subscriptions_with_existing_gauges(self):
        self.seed()
        output = self.render()
        self.assertIn('chatgpt', output)
        self.assertIn('13%', output)
        self.assertIn('cursor', output)
        self.assertIn('0%', output)
        self.assertNotIn('~', output)
        self.assertNotIn('?', output)

    def test_missing_and_expired_readings_are_not_zero(self):
        self.assertEqual(self.render(), 'chatgpt ?  cursor ?')
        self.seed(reset=1)
        self.assertEqual(self.render(), 'chatgpt ?  cursor ?')

    def test_failed_or_old_readings_marked_stale(self):
        for status, age in [('network', 0), ('ok', 200)]:
            self.seed(status=status, age=age)
            self.assertEqual(self.render().count('~'), 2)


if __name__ == '__main__':
    unittest.main()
