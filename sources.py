"""Catalogue adapters based on public markup observed 2026-11-17.

No hidden endpoints, login, or Telegram username inference. Published slug/code
fields identify agency cards; official websites come from designated site fields.
"""
from __future__ import annotations

import base64
import html as html_lib
import json
import re
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qs, urlencode
from bs4 import BeautifulSoup

SOURCES = {
    'workspace': 'https://workspace.ru/contractors/',
    'ruward': 'https://ruward.ru/catalog/',
    'yandex': 'https://b2b.yandex.ru/adv/contact/agencies',
    'cmsmagazine': 'https://cmsmagazine.ru/agencies/',
}
_CATALOGUES = {'workspace.ru', 'ruward.ru', 'cmsmagazine.ru', 'b2b.yandex.ru',
               'ratingruneta.ru', 'ga4.ratingruneta.ru', 'api.cmsmagazine.ru'}
_SOCIAL = {'t.me', 'telegram.me', 'telegram.dog', 'vk.com', 'max.ru', 'youtube.com',
           'youtu.be', 'dzen.ru', 'facebook.com', 'instagram.com', 'twitter.com',
           'x.com', 'rutube.ru', 'ok.ru', 'yandex.ru', 'yandex.com', 'google.com'}


def _clean_website(value: str, base: str) -> str | None:
    value = html_lib.unescape(str(value or '')).strip()
    if not value or re.search(r'\s', value):
        return None
    if not value.lower().startswith(('http://', 'https://', '//')):
        if not re.match(r'^[\w.-]+\.[a-zа-я]{2,}(?:[/:?]|$)', value, re.I):
            return None
        value = 'https://' + value
    value = urljoin(base, value)
    try:
        p = urlsplit(value)
        host = (p.hostname or '').lower().removeprefix('www.')
        if not host or p.username or p.password or p.scheme not in ('http', 'https'):
            return None
        if host in _CATALOGUES | _SOCIAL or any(host.endswith('.' + h) for h in _CATALOGUES | _SOCIAL):
            return None
        # Tracking parameters are irrelevant to identifying the official website.
        query = {k: v for k, v in parse_qs(p.query, keep_blank_values=True).items()
                 if not k.lower().startswith(('utm_', 'yclid', 'gclid'))}
        return urlunsplit((p.scheme, p.netloc, p.path or '/', urlencode(query, doseq=True), ''))
    except ValueError:
        return None


def _unique(seq):
    return list(dict.fromkeys(seq))


def _next_data(soup):
    el = soup.find('script', id='__NEXT_DATA__')
    if el:
        try:
            return json.loads(el.string or el.get_text()).get('props', {}).get('pageProps', {})
        except (ValueError, TypeError):
            pass
    return {}


def _cms_state(soup):
    for el in soup.find_all('script'):
        text = el.string or el.get_text()
        if re.match(r'\s*window\.__INITIAL_STATE__\s*=', text):
            raw = text.split('=', 1)[1].strip().rstrip(';')
            # The public inline JS uses \< in HTML strings; JS permits it but JSON
            # does not. Drop only invalid escape backslashes, never execute JS.
            raw = re.sub(r'\\(?!["\\/bfnrtu])', '', raw)
            try:
                return json.loads(raw)
            except (ValueError, TypeError):
                return {}
    return {}


def published_href(a, base):
    """Read a catalogue's explicitly published destination, without visiting its tracker."""
    href = a.get('href', '')
    resolved = urljoin(base, href)
    if urlsplit(resolved).path.startswith('/redirect/'):
        explicit = str(a.get('data-analytics-id', ''))
        if explicit.startswith(('https://', 'http://')):
            return explicit
        raw = parse_qs(urlsplit(resolved).query).get('url', [''])[0]
        if raw:
            try:
                decoded = html_lib.unescape(base64.b64decode(raw, validate=True).decode('utf-8'))
                if decoded.startswith(('https://', 'http://')):
                    return decoded
            except (ValueError, UnicodeError):
                pass
    return resolved


def _cms_next(response, params):
    next_url = response.get('next_page_url')
    if not isinstance(next_url, str):
        return []
    next_page = parse_qs(urlsplit(next_url).query).get('page', [''])[0]
    company_type = params.get('company_type_code', '')
    if not next_page.isdigit() or not re.fullmatch(r'[\w-]+', company_type):
        return []
    query = {k: v for k, v in params.items() if v is not None and k in
             ('company_type_code', 'sort', 'sort_direction', 'path')}
    query['page'] = next_page
    return ['https://api.cmsmagazine.ru/v1/companiesList?' + urlencode(query)]


def discover(html: str, url: str, source: str) -> dict:
    """Return card URLs, pagination/category URLs, and agency-only inline blocks."""
    # This unauthenticated JSON endpoint is used by CMS's public frontend for
    # its 'Show more' control. Never parse arbitrary APIs as catalogue content.
    p = urlsplit(url)
    if source == 'cmsmagazine' and p.hostname == 'api.cmsmagazine.ru' and p.path == '/v1/companiesList':
        try:
            response = json.loads(html)
        except (TypeError, ValueError):
            return {'cards': [], 'pages': [], 'inline': []}
        cards = []
        for item in response.get('data', []):
            code = item.get('code', '')
            if re.fullmatch(r'[\w-]+', code):
                cards.append({'url': 'https://cmsmagazine.ru/agencies/' + code + '/',
                              'company': str(item.get('title', ''))})
        pages = _cms_next(response, {k:v[-1] for k,v in parse_qs(p.query).items()})
        return {'cards': cards, 'pages': pages, 'inline': []}
    soup = BeautifulSoup(html, 'html.parser')
    cards, pages, inline = [], [], []
    seen = set()

    def card(link, name=''):
        link = urljoin(url, link)
        if link not in seen:
            seen.add(link)
            cards.append({'url': link, 'company': str(name or '').strip()})
        elif name:
            for item in cards:
                if item['url'] == link and not item['company']:
                    item['company'] = str(name).strip()

    patterns = {
        'workspace': r'^/contractors/[^/?#]+/$',
        'ruward': r'^/catalog/[^/?#]+/$',
        'yandex': r'^/adv/contact/agencies/[^/?#]+/?$',
        'cmsmagazine': r'^/agencies/[^/?#]+/$',
    }
    for a in soup.select('a[href]'):
        href = a.get('href', '')
        p = urlsplit(urljoin(url, href))
        if p.hostname == urlsplit(SOURCES[source]).hostname and re.fullmatch(patterns[source], p.path):
            card(urlunsplit((p.scheme, p.netloc, p.path, '', '')),
                 a.get_text(' ', strip=True) or a.get('title', ''))
        if source == 'ruward' and re.fullmatch(r'/catalog/p\d+\.html', p.path):
            pages.append(urljoin(url, href))
    for a in soup.select('link[rel~=next][href], a[rel~=next][href]'):
        p = urljoin(url, a['href'])
        if urlsplit(p).hostname == urlsplit(url).hostname:
            pages.append(p)

    if source == 'workspace':
        if urlsplit(url).path.rstrip('/') == '/contractors':
            pages.extend(urljoin(url, a['href']) for a in soup.select('.categories__card-list a[href]'))
        # Priority cards also publish official sites directly. They still have a
        # normal card URL elsewhere on the page, so only the card queue is used.
    elif source == 'cmsmagazine':
        state = _cms_state(soup)
        remote = state.get('remoteData', {})
        if urlsplit(url).path.rstrip('/') == '/agencies':
            types = remote.get('companyTypesFull', {}).get('response', [])
            for item in types:
                code = item.get('code', '')
                if re.fullmatch(r'[\w-]+', code):
                    pages.append(urljoin(url, '/' + code + '/'))
        response = remote.get('catalogListPage', {}).get('response', {})
        for item in response.get('data', []):
            code = item.get('code', '')
            if re.fullmatch(r'[\w-]+', code):
                card('/agencies/' + code + '/', item.get('title', ''))
        request = remote.get('catalogListPage', {}).get('request', {})
        if request.get('url', '').lstrip('/') == 'companiesList':
            pages.extend(_cms_next(response, request.get('params', {})))
    elif source == 'yandex':
        props = _next_data(soup)
        agencies = props.get('initialStoreState', {}).get('agencies', {})
        for item in agencies.get('data', []):
            attrs = item.get('attributes', {})
            slug = attrs.get('slug', '')
            if re.fullmatch(r'[\w-]+', slug):
                card('/adv/contact/agencies/' + slug, attrs.get('name', ''))
        pag = agencies.get('meta', {}).get('pagination', {})
        start, limit, total = (pag.get(k, 0) for k in ('start', 'limit', 'total'))
        if all(isinstance(x, int) for x in (start, limit, total)) and limit > 0 and start + limit < total:
            p = urlsplit(url)
            q = parse_qs(p.query)
            q['page'] = [str(start // limit + 2)]
            pages.append(urlunsplit((p.scheme, p.netloc, p.path, urlencode(q, doseq=True), '')))
    return {'cards': cards, 'pages': _unique(pages), 'inline': inline}


def card_info(html: str, url: str, source: str) -> dict:
    soup = BeautifulSoup(html, 'html.parser')
    h1 = soup.find('h1')
    company = h1.get_text(' ', strip=True) if h1 else ''
    sites = []
    selectors = {
        'workspace': '.card-info__contact._site',
        'ruward': '.mainurl a[href], .aliases a[href]',
        'yandex': 'a.ContactsRow__link[href]',
        'cmsmagazine': 'div._3LwepWoDjL a[href]',
    }
    for a in soup.select(selectors[source]):
        site = _clean_website(published_href(a, url), url)
        if site:
            sites.append(site)
    # Parse only the agency's Organization JSON-LD, excluding catalogue identity.
    for el in soup.select('script[type="application/ld+json"]'):
        try:
            obj = json.loads(el.string or el.get_text())
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and obj.get('@type') == 'Organization' and obj.get('name', '').strip() == company:
            for value in obj.get('sameAs', []):
                site = _clean_website(value, url)
                if site:
                    sites.append(site)
    if source == 'yandex':
        data = _next_data(soup).get('initialStoreState', {})
        # Known card contact fields remain visible in markup; do not walk all
        # arbitrary website fields in the state (which also contains navigation).
    root = soup
    if source == 'workspace':
        root = soup.select_one('[data-detail-company-block]') or soup
    elif source == 'ruward':
        root = soup.select_one('#companycommoncontent') or soup.select_one('.companycommoncontent') or soup
    elif source == 'yandex':
        # The catalogue footer is a div[data-testid=Footer], not a <footer>.
        # Extract the agency's explicitly named region; never fall back to the
        # whole document when that region changes or is absent.
        root = (soup.select_one('[data-testid="AgencyHeroBanner"]') or
                soup.select_one('[data-testid="ProfileContacts"]') or
                BeautifulSoup('', 'html.parser'))
    # Strip catalogue navigation and unrelated material before Telegram extraction.
    for el in list(root.select('footer, header, nav, script, style, .footer, [data-footer], [data-testid="Footer"], .redcollarlow')):
        el.decompose()
    # Replace only published redirect destinations; no tracker is fetched.
    for a in root.select('a[href]'):
        a['href'] = published_href(a, url)
    return {'company': company, 'websites': _unique(sites), 'html': str(root)}
