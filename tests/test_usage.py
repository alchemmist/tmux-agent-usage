import itertools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='usage-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / '.cache/claude-usage'
        self.cache.mkdir(parents=True)
        self.sessions = self.root / 'codex/sessions'
        self.sessions.mkdir(parents=True)
        for name in ('segment.sh', 'codex.sh', 'helpers.sh'):
            shutil.copy(REPO / 'scripts' / name, self.root)
        helpers = self.root / 'helpers.sh'
        helpers.write_text(helpers.read_text().replace('"$HOME"', f'"{self.root}"'))
        self.socket = str(self.root / 'socket')
        self.tmux('new-session', '-d')
        self.addCleanup(self.tmux, 'kill-server')
        wrapper = self.root / 'tmux'
        wrapper.write_text(f'#!/bin/sh\nexec {shutil.which("tmux")} -S {self.socket} "$@"\n')
        wrapper.chmod(0o755)
        self.env = dict(os.environ, PATH=f'{self.root}:{os.environ["PATH"]}', CODEX_HOME=str(self.root / 'codex'))
        self.now = int(time.time())

    def tmux(self, *args):
        return subprocess.check_output(['tmux', '-S', self.socket, '-f', '/dev/null', *args], text=True)

    def option(self, key, value):
        self.tmux('set-option', '-g', '@claude_usage_' + key, value)

    def render(self):
        output = subprocess.check_output(['bash', str(self.root / 'segment.sh')], env=self.env, text=True)
        return re.sub(r'#\[.*?\]', '', output)

    def seed(self):
        (self.cache / 'usage').write_text(f'FIVE_HOUR_PCT=11\nSEVEN_DAY_PCT=22\nUPDATED_AT={self.now}\n')
        (self.cache / 'codex').write_text(f'CODEX_CURRENT_PCT=33\nCODEX_WEEK_PCT=44\nUPDATED_AT={self.now}\n')
        self.option('style', 'gauge')

    def test_assistant_and_window_combinations(self):
        self.seed()
        for claude, codex, cmode, xmode in itertools.product(('on', 'off'), ('on', 'off'), ('current', 'weekly', 'all'), ('current', 'weekly', 'all')):
            with self.subTest(claude=claude, codex=codex, cmode=cmode, xmode=xmode):
                for key, value in [('claude', claude), ('codex', codex), ('claude_show', cmode), ('codex_show', xmode)]:
                    self.option(key, value)
                output = self.render()
                for reading, visible in [('11%', claude == 'on' and cmode != 'weekly'), ('22%', claude == 'on' and cmode != 'current'), ('33%', codex == 'on' and xmode != 'weekly'), ('44%', codex == 'on' and xmode != 'current')]:
                    self.assertEqual(reading in output, visible, output)

    def test_legacy_options_and_explicit_override(self):
        self.seed()
        self.option('show', 'weekly')
        self.assertIn('22%', self.render())
        self.assertNotIn('11%', self.render())
        self.option('codex_only', 'on')
        self.assertEqual('', self.render())
        self.option('claude', 'on')
        self.option('claude_show', 'current')
        self.assertIn('11%', self.render())
        self.assertNotIn('22%', self.render())

    def test_hidden_claude_has_no_stale_marker(self):
        self.seed()
        (self.cache / 'usage').write_text('FIVE_HOUR_PCT=11\nUPDATED_AT=1\n')
        self.option('claude', 'off')
        self.option('codex', 'on')
        self.option('stale_after', '1')
        self.assertNotIn('stale', self.render())

    def harvest(self, windows):
        (self.cache / 'codex').unlink(missing_ok=True)
        event = {'payload': {'rate_limits': windows}}
        (self.sessions / 'rollout-new.jsonl').write_text(json.dumps(event) + '\n')
        subprocess.run(['bash', str(self.root / 'codex.sh')], env=self.env, check=True)
        cache = self.cache / 'codex'
        return dict(line.split('=', 1) for line in cache.read_text().splitlines()) if cache.exists() else {}

    def test_codex_window_selection(self):
        short = {'window_minutes': 300, 'used_percent': 33, 'resets_at': self.now + 3600}
        week = {'window_minutes': 10080, 'used_percent': 44, 'resets_at': self.now + 86400}
        for primary, secondary, current, weekly in [(short, week, '33', '44'), (week, short, '33', '44'), (week, None, '', '44'), (short, None, '33', ''), (None, None, None, None)]:
            with self.subTest(primary=primary, secondary=secondary):
                cache = self.harvest({'primary': primary, 'secondary': secondary})
                self.assertEqual(cache.get('CODEX_CURRENT_PCT'), current)
                self.assertEqual(cache.get('CODEX_WEEK_PCT'), weekly)

    def test_newest_session_wins_without_mixing_windows(self):
        old = self.sessions / 'rollout-old.jsonl'
        old.write_text(json.dumps({'payload': {'rate_limits': {'primary': {'window_minutes': 10080, 'used_percent': 99, 'resets_at': self.now + 999999}}}}) + '\n')
        os.utime(old, (self.now - 100, self.now - 100))
        cache = self.harvest({'primary': {'window_minutes': 300, 'used_percent': 12}})
        self.assertEqual(cache['CODEX_CURRENT_PCT'], '12')
        self.assertEqual(cache['CODEX_WEEK_PCT'], '')
        self.option('claude', 'off')
        self.option('codex', 'on')
        self.option('codex_show', 'all')
        self.assertIn('12%', self.render())
        self.assertNotIn('99%', self.render())


if __name__ == '__main__':
    unittest.main()
