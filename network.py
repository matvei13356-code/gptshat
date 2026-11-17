"""Bounded HTTP transport; no browser, retries, login, CAPTCHA solving or proxy fallback."""
from __future__ import annotations

import ipaddress
import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from protego import Protego

import requests

UA = 'AgencyContactCollector/1.0'
BLOCK_CODES = {401, 403, 407, 423, 429, 451, 503}
MAX_BYTES = 5 * 1024 * 1024


class FetchError(Exception):
    pass


class BudgetStop(Exception):
    pass


def safe_url(raw: str, base: str = '') -> str | None:
    try:
        p = urlsplit(urljoin(base, raw.strip()))
        host = (p.hostname or '').lower().rstrip('.')
        if p.scheme not in {'http', 'https'} or not host or p.username or p.password:
            return None
        if p.port not in {None, 80, 443} or '.' not in host or host.endswith(('.local', '.localhost', '.internal')):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        host = host.encode('idna').decode('ascii')
        netloc = host + (f':{p.port}' if p.port else '')
        return urlunsplit((p.scheme, netloc, p.path or '/', p.query, ''))
    except (ValueError, UnicodeError):
        return None


def origin(url: str) -> str:
    p = urlsplit(url)
    return f'{p.scheme}://{p.netloc}'


def gate_page(html: str, url: str) -> bool:
    # Check visible challenge text/title, not incidental mentions in scripts or footers.
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'noscript']):
        tag.decompose()
    title = soup.title.get_text(' ', strip=True).lower() if soup.title else ''
    text = soup.get_text(' ', strip=True).lower()
    if re.search(r'/(?:captcha|showcaptcha|login|sign-in|signin|auth)(?:/|\?|$)', url, re.I):
        return True
    if re.search(r'captcha|access denied|доступ запрещ|just a moment|проверка браузера|подтвердите.*человек|robot check', title):
        return True
    phrases = ('подтвердите, что вы не робот', 'подтвердите что вы не робот',
               'verify you are human', 'checking your browser', 'доступ временно ограничен',
               'доступ с вашего ip', 'для продолжения необходимо авторизоваться')
    if any(s in text for s in phrases):
        return True
    # A small page whose main content is a login form is not an agency page.
    return bool(len(text) < 1200 and soup.select_one('input[type="password"]'))


class Transport:
    def __init__(self, db, data: Path, delay=2.5, jitter=1.0, timeout=25, proxy=None,
                 max_requests=1000):
        self.db, self.data = db, data
        self.delay, self.jitter, self.timeout = delay, jitter, timeout
        self.max_requests, self.count = max_requests, 0
        self.last_request = 0.0
        self.parsers = {}
        self.robot_delays = {}
        self.session = requests.Session()
        self.session.headers['User-Agent'] = UA
        if proxy:
            p = urlsplit(proxy)
            if p.scheme not in {'http', 'https', 'socks5h'} or not p.hostname or not p.port:
                raise ValueError('Proxy: use http://host:port, https://host:port or socks5h://host:port')
            self.session.trust_env = False  # NO_PROXY must never bypass the explicit proxy.
            self.session.proxies = {'http': proxy, 'https': proxy}
        # Without an explicit proxy, keep the environment's normal transport/CA settings.

    def close(self):
        self.session.close()

    def event(self, **item):
        item['time_utc'] = datetime.now(timezone.utc).isoformat()
        with (self.data / 'requests.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(item, ensure_ascii=False) + '\n')

    def block(self, url, reason):
        host = urlsplit(url).hostname
        self.db.execute('INSERT OR REPLACE INTO blocked(host,reason) VALUES (?,?)', (host, reason))
        self.db.commit()

    def is_blocked(self, url):
        return self.db.execute('SELECT reason FROM blocked WHERE host=?',
                               (urlsplit(url).hostname,)).fetchone()

    def _request(self, url):
        if self.count >= self.max_requests:
            raise BudgetStop()
        blocked = self.is_blocked(url)
        if blocked:
            raise FetchError('host_stopped: ' + blocked[0])
        pause = max(self.delay, self.robot_delays.get(origin(url), 0)) + random.uniform(0, self.jitter)
        time.sleep(max(0, self.last_request + pause - time.monotonic()))
        self.count += 1
        self.last_request = time.monotonic()
        started = time.monotonic()
        try:
            with self.session.get(url, timeout=(min(15, self.timeout), self.timeout),
                                  allow_redirects=False, stream=True) as response:
                status = response.status_code
                meta = {'url': url, 'status': status,
                        'elapsed_s': round(time.monotonic() - started, 3)}
                if status in BLOCK_CODES:
                    reason = f'HTTP {status}'
                    if response.headers.get('Retry-After'):
                        meta['retry_after'] = response.headers['Retry-After']
                    self.block(url, reason)
                    self.event(**meta, result='stopped')
                    raise FetchError(reason)
                if 300 <= status < 400:
                    self.event(**meta, result='redirect')
                    return status, response.headers, ''
                if status != 200:
                    self.event(**meta, result='http_error')
                    raise FetchError(f'HTTP {status}')
                size = 0
                chunks = []
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        self.event(**meta, result='too_large')
                        raise FetchError('response_larger_than_5_MiB')
                    chunks.append(chunk)
                content = b''.join(chunks)
                encoding = response.encoding
                # requests defaults text/* without charset to latin1; Russian pages are usually UTF-8.
                if not encoding or encoding.lower() == 'iso-8859-1':
                    encoding = requests.utils.get_encodings_from_content(content[:10000].decode('ascii', 'ignore'))
                    encoding = encoding[0] if encoding else 'utf-8'
                try:
                    body = content.decode(encoding, errors='replace')
                except LookupError:
                    body = content.decode('utf-8', errors='replace')
                self.event(**meta, result='ok', bytes=size)
                return status, response.headers, body
        except requests.RequestException as exc:
            # Exception strings may contain proxy passwords; only record the exception class.
            name = type(exc).__name__
            self.event(url=url, status=None, result=name)
            self.block(url, name)
            raise FetchError(name) from None

    def _robots(self, url):
        key = origin(url)
        if key in self.parsers:
            return self.parsers[key]
        robots_url = key + '/robots.txt'
        visited = set()
        try:
            for _ in range(6):
                if robots_url in visited:
                    raise FetchError('robots_redirect_loop')
                visited.add(robots_url)
                status, headers, text = self._request(robots_url)
                if 300 <= status < 400:
                    target = safe_url(headers.get('Location', ''), robots_url)
                    old = urlsplit(robots_url)
                    new = urlsplit(target or '')
                    canonical_upgrade = (new.scheme == 'https'
                                         and (new.hostname or '').removeprefix('www.')
                                         == (old.hostname or '').removeprefix('www.'))
                    if not target or (origin(target) != key and not canonical_upgrade):
                        raise FetchError('robots_cross_origin_redirect')
                    robots_url = target
                    continue
                if '<html' in text[:300].lower() or gate_page(text, robots_url):
                    self.block(url, 'robots_challenge_or_html')
                    raise FetchError('robots_challenge_or_html')
                rp = Protego.parse(text.lstrip('\ufeff'))
                self.parsers[key] = rp
                self.robot_delays[key] = rp.crawl_delay(UA) or 0
                rate = rp.request_rate(UA)
                if rate:
                    self.robot_delays[key] = max(self.robot_delays[key], rate.seconds / rate.requests)
                return rp
            raise FetchError('robots_too_many_redirects')
        except FetchError as exc:
            if str(exc) in {'HTTP 404', 'HTTP 410'}:
                rp = Protego.parse('')
                self.parsers[key] = rp
                return rp
            # Fail closed if robots could not be read; cache denial for this run.
            self.parsers[key] = None
            raise FetchError('robots_unavailable: ' + str(exc)) from None

    def fetch(self, raw):
        url = safe_url(raw)
        if not url:
            raise FetchError('unsafe_or_unsupported_url')
        visited = set()
        for _ in range(6):
            if url in visited:
                raise FetchError('redirect_loop')
            visited.add(url)
            if self.is_blocked(url):
                raise FetchError('host_stopped')
            rp = self._robots(url)
            if rp is None or not rp.can_fetch(url, UA):
                self.event(url=url, status=None, result='robots_disallowed')
                raise FetchError('robots_disallowed')
            status, headers, text = self._request(url)
            if 300 <= status < 400:
                url = safe_url(headers.get('Location', ''), url)
                if not url:
                    raise FetchError('unsafe_redirect')
                continue
            ctype = headers.get('Content-Type', '').lower()
            public_cms_json = (urlsplit(url).hostname == 'api.cmsmagazine.ru'
                               and urlsplit(url).path == '/v1/companiesList'
                               and 'application/json' in ctype)
            if ctype and not public_cms_json and not any(x in ctype for x in ('text/html', 'application/xhtml+xml')):
                raise FetchError('non_html')
            if gate_page(text, url):
                self.block(url, 'challenge_or_login')
                raise FetchError('challenge_or_login')
            return url, text
        raise FetchError('too_many_redirects')
