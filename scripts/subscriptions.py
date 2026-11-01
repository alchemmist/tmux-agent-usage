#!/usr/bin/env python3
import argparse
import base64
import concurrent.futures
import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import time


class UsageError(Exception):
    pass


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def percentage(value):
    if not number(value) or value < 0:
        raise UsageError('invalid_usage')
    return min(100, value)


def epoch(value):
    if number(value) and value > 0:
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp())
        except ValueError:
            pass
    return ''


def parse_chatgpt(data):
    limits = data.get('rate_limit') or {}
    result = {}
    for key in ('primary_window', 'secondary_window'):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        duration = window.get('limit_window_seconds')
        if not number(duration) or duration <= 0:
            continue
        name = 'WEEK' if duration >= 86400 else 'CURRENT'
        result[name + '_PCT'] = percentage(window.get('used_percent'))
        result[name + '_RESET'] = epoch(window.get('reset_at'))
    if not result:
        raise UsageError('missing_usage')
    return result


def parse_cursor(data):
    plan = (data.get('individualUsage') or {}).get('plan') or {}
    pct = plan.get('totalPercentUsed')
    if pct is None:
        used, limit = plan.get('used'), plan.get('limit')
        if not number(used) or not number(limit) or limit <= 0:
            raise UsageError('missing_usage')
        pct = used / limit * 100
    return {'PCT': percentage(pct), 'RESET': epoch(data.get('billingCycleEnd'))}


def claims(token):
    try:
        return json.loads(base64.urlsafe_b64decode(token.split('.')[1] + '==='))
    except (ValueError, IndexError):
        raise UsageError('invalid_auth') from None


def load_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def request(url, headers, resolve=''):
    args = ['curl', '--silent', '--show-error', '--fail', '--connect-timeout', '5', '--max-time', '15', '--max-filesize', '1048576', '--header', '@-', '--write-out', '\n%{http_code}', url]
    if resolve:
        args += ['--resolve', resolve]
    try:
        response = subprocess.run(args, input='\n'.join(headers) + '\n', capture_output=True, text=True, timeout=18)
    except (OSError, subprocess.TimeoutExpired):
        raise UsageError('network') from None
    body, _, status = response.stdout.rpartition('\n')
    if response.returncode:
        raise UsageError('auth' if status in ('401', '403') else 'network')
    try:
        data = json.loads(body)
    except ValueError:
        raise UsageError('invalid_response') from None
    if not isinstance(data, dict):
        raise UsageError('invalid_response')
    return data


def refresh_chatgpt_auth(home):
    binary = shutil.which('antex') or str(home / '.local/bin/antex')
    if not Path(binary).is_file():
        raise UsageError('auth')
    environment = dict(os.environ, ANTEX_HOME=str(os.environ.get('CODEX_HOME', home / '.antex')))
    process = subprocess.Popen([binary, 'app-server', '--stdio'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        messages = [
            {'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'tmux-agent-usage', 'version': '1.0'}}},
            {'method': 'initialized', 'params': {}},
            {'id': 2, 'method': 'account/read', 'params': {'refreshToken': True}},
        ]
        process.stdin.write(('\n'.join(json.dumps(message) for message in messages) + '\n').encode())
        process.stdin.flush()
        deadline = time.monotonic() + 20
        pending = b''
        while time.monotonic() < deadline:
            if not selector.select(max(0, deadline - time.monotonic())):
                break
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                response = json.loads(line)
                if response.get('id') == 2:
                    if 'error' in response:
                        raise UsageError('auth')
                    return
        raise UsageError('auth')
    finally:
        selector.close()
        process.stdin.close()
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()


def fetch_chatgpt(home):
    paths = [Path(os.environ.get('CODEX_HOME', home / '.antex')) / 'auth.json', home / '.codex/auth.json']
    credentials = []
    for path in dict.fromkeys(paths):
        tokens = load_json(path).get('tokens') or {}
        if tokens.get('access_token'):
            token_claims = claims(tokens['access_token'])
            if path.parent == Path(os.environ.get('CODEX_HOME', home / '.antex')) and token_claims.get('exp', 0) <= time.time() + 60:
                refresh_chatgpt_auth(home)
                tokens = load_json(path).get('tokens') or {}
            if tokens.get('access_token'):
                credentials.append((tokens['access_token'], tokens.get('account_id')))
    pi = load_json(home / '.pi/agent/auth.json').get('openai') or {}
    if pi.get('access'):
        token = pi['access']
        account = claims(token).get('https://api.openai.com/auth', {}).get('chatgpt_account_id')
        credentials.append((token, account))
    error = UsageError('missing_auth')
    for token, account in credentials:
        headers = ['Authorization: Bearer ' + token]
        if account:
            headers.append('ChatGPT-Account-Id: ' + account)
        try:
            return parse_chatgpt(request('https://chatgpt.com/backend-api/wham/usage', headers))
        except UsageError as exc:
            error = exc
            if str(exc) != 'auth':
                break
    raise error


def fetch_cursor(home, resolve):
    auth = load_json(home / '.pi/agent/auth.json').get('cursor') or {}
    token = auth.get('access')
    if not token:
        raise UsageError('missing_auth')
    subject = claims(token).get('sub', '').split('|')[-1]
    if not re.fullmatch(r'[A-Za-z0-9._-]+', subject):
        raise UsageError('invalid_auth')
    cookie = 'Cookie: WorkosCursorSessionToken=' + subject + '%3A%3A' + token
    return parse_cursor(request('https://cursor.com/api/usage-summary', [cookie], resolve))


def read_cache(path):
    try:
        return dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)
    except OSError:
        return {}


def refresh(provider, home, cache, ttl, force, resolve):
    path = cache / provider
    with (cache / (provider + '.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        old = read_cache(path)
        now = int(time.time())
        attempted = old.get('ATTEMPT_AT', '0')
        if not force and attempted.isdigit() and 0 <= now - int(attempted) < ttl:
            return
        try:
            values = fetch_chatgpt(home) if provider == 'chatgpt' else fetch_cursor(home, resolve)
            values.update(UPDATED_AT=now, STATUS='ok')
        except UsageError as exc:
            values = dict(old, STATUS=str(exc))
        except (OSError, ValueError, TypeError, AttributeError):
            values = dict(old, STATUS='invalid_response')
        values['ATTEMPT_AT'] = now
        temp = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
        temp.write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
        temp.chmod(0o600)
        temp.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--providers', default='chatgpt,cursor')
    parser.add_argument('--ttl', type=int, default=60)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--cursor-resolve', default='')
    args = parser.parse_args()
    home = Path.home()
    cache = home / '.cache/claude-usage/subscriptions'
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    providers = [p for p in dict.fromkeys(args.providers.split(',')) if p in ('chatgpt', 'cursor')]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(refresh, p, home, cache, max(15, args.ttl), args.force, args.cursor_resolve) for p in providers]
        for future in futures:
            future.result()


if __name__ == '__main__':
    main()
