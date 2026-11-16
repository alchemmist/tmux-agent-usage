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
import subprocess
import time
import urllib.parse


class UsageError(Exception):
    pass


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def percentage(value):
    if not number(value) or value < 0:
        raise UsageError('invalid_usage')
    return min(100, value)


def epoch(value):
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if number(value) and value > 0:
        if value > 10**12:
            value /= 1000
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
    plan = data.get('planUsage') or (data.get('individualUsage') or {}).get('plan') or {}
    pct = plan.get('totalPercentUsed')
    if pct is None:
        used, limit = plan.get('used'), plan.get('limit')
        if not number(used) or not number(limit) or limit <= 0:
            raise UsageError('missing_usage')
        pct = used / limit * 100
    reset = data.get('billingCycleEnd')
    if reset in (None, ''):
        reset = (data.get('planInfo') or {}).get('includedUsageResetsAt')
    return {'PCT': percentage(pct), 'RESET': epoch(reset)}


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


def request(url, headers, resolve='', body=None):
    args = ['curl', '--silent', '--show-error', '--fail', '--connect-timeout', '5', '--max-time', '15', '--max-filesize', '1048576', '--header', '@-', '--write-out', '\n%{http_code}']
    if body is not None:
        args += ['--request', 'POST', '--data', body]
    args.append(url)
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


def pi_auth_path(home):
    return home / '.pi/agent/auth.json'


def refresh_pi_openai(home):
    path = pi_auth_path(home)
    data = load_json(path)
    cred = data.get('openai') or {}
    client_id, refresh_token = cred.get('clientId'), cred.get('refresh')
    if not client_id or not refresh_token:
        raise UsageError('auth')
    body = urllib.parse.urlencode({
        'grant_type': 'refresh_token',
        'client_id': client_id,
        'refresh_token': refresh_token,
        'resource': 'https://api.openai.com/v1',
    })
    token = request('https://auth.openai.com/api/accounts/oauth/token',
                    ['Content-Type: application/x-www-form-urlencoded', 'Accept: application/json'], body=body)
    access, refresh = token.get('access_token'), token.get('refresh_token')
    expires_in = token.get('expires_in')
    if not isinstance(access, str) or not isinstance(refresh, str) or not number(expires_in):
        raise UsageError('auth')
    cred['access'] = access
    cred['refresh'] = refresh
    cred['expires'] = int(time.time() * 1000 + expires_in * 1000 - 180000)
    cred['type'] = cred.get('type') or 'oauth'
    data['openai'] = cred
    temp = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    temp.write_text(json.dumps(data))
    temp.chmod(0o600)
    temp.replace(path)
    return cred


def fetch_chatgpt(home):
    path = pi_auth_path(home)
    data = load_json(path)
    cred = data.get('openai-codex') or data.get('openai') or {}
    token = cred.get('access')
    if not token:
        raise UsageError('missing_auth')
    expires = cred.get('expires')
    if data.get('openai-codex') is None and number(expires) and expires <= (time.time() + 60) * 1000:
        cred = refresh_pi_openai(home)
        token = cred.get('access')
    if not token:
        raise UsageError('auth')
    account = cred.get('accountId') or claims(token).get('https://api.openai.com/auth', {}).get('chatgpt_account_id')
    headers = ['Authorization: Bearer ' + token]
    if account:
        headers.append('ChatGPT-Account-Id: ' + account)
    return parse_chatgpt(request('https://chatgpt.com/backend-api/wham/usage', headers))


def fetch_cursor(home, resolve):
    auth = load_json(home / '.pi/agent/auth.json').get('cursor') or {}
    token = auth.get('access')
    if not token:
        raise UsageError('missing_auth')
    subject = claims(token).get('sub', '').split('|')[-1]
    if not re.fullmatch(r'[A-Za-z0-9._-]+', subject):
        raise UsageError('invalid_auth')
    cookie = 'Cookie: WorkosCursorSessionToken=' + subject + '%3A%3A' + token
    headers = [cookie, 'Origin: https://cursor.com', 'Content-Type: application/json']
    return parse_cursor(request('https://cursor.com/api/dashboard/get-current-period-usage', headers, resolve, '{}'))


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
            if provider == 'chatgpt' and str(exc) in ('auth', 'missing_auth'):
                for key in list(values):
                    if key.endswith('_PCT') or key.endswith('_RESET'):
                        values.pop(key)
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
