#!/usr/bin/env python3
"""Collect explicitly published Telegram contact links from four agency directories."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import tldextract
from bs4 import BeautifulSoup

from network import BudgetStop, FetchError, Transport, safe_url
from sources import SOURCES, card_info, discover
from telegram_rules import canonical_telegram, extract_telegram

DEFAULTS = dict(delay=2.5, jitter=1.0, timeout=25, max_requests=1000,
                max_agencies_per_source=100, max_listing_pages=30,
                max_site_pages=8, max_site_depth=2, proxy=None, require_proxy=False)
DOMAIN = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)
INFO_PATH = re.compile(r'(?:contacts?|kontakty?|kontakti|about(?:-us|-company)?|'
                       r'company|o[-_]kompanii|o[-_]nas|partners?|partnerships?|partnery|partneram|'
                       r'sotrudnichestvo|контакты|о-компании|партн[её]р(?:ам|ы))', re.I)
INFO_LABEL = re.compile(r'(?:наши\s+)?контакты|о\s+(?:нас|компании)|партн[её]р(?:ам|ы)|'
                        r'стать\s+партн[её]ром|связаться(?:\s+с\s+нами)?|'
                        r'contacts?|about(?:\s+us)?|partners?|partnership', re.I)


def domain(url):
    host = (urlsplit(url).hostname or '').lower().rstrip('.')
    item = DOMAIN(host)
    return item.top_domain_under_public_suffix or host


def site_links(html, url, home):
    soup = BeautifulSoup(html, 'html.parser')
    result = []
    for a in soup.find_all('a', href=True):
        link = safe_url(str(a['href']), url)
        if not link or domain(link) != domain(home):
            continue
        path = unquote(urlsplit(link).path)
        if '://' in path or any(k.lower().startswith(('back_url', 'logout', 'sessid'))
                                for k in parse_qs(urlsplit(link).query)):
            continue
        last = re.sub(r'\.(?:html?|php|aspx)$', '', path.rstrip('/').rsplit('/', 1)[-1])
        label = a.get_text(' ', strip=True)
        title = str(a.get('title', ''))
        if not (INFO_PATH.fullmatch(last) or INFO_LABEL.fullmatch(label) or INFO_LABEL.fullmatch(title)):
            continue
        if re.search(r'\.(?:pdf|jpg|jpeg|png|gif|zip|docx?|xlsx?)(?:$|\?)', link, re.I):
            continue
        result.append((0 if re.search('контакт|contact|kontak', last + ' ' + label, re.I) else 1, link))
    return list(dict.fromkeys(link for _, link in sorted(result)))


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS tasks(
            id INTEGER PRIMARY KEY, kind TEXT, url TEXT, source TEXT, company TEXT DEFAULT '',
            home TEXT DEFAULT '', depth INTEGER DEFAULT 0, status TEXT DEFAULT 'pending',
            error TEXT DEFAULT '', final_url TEXT DEFAULT '',
            UNIQUE(kind,url,source,company));
        CREATE TABLE IF NOT EXISTS agencies(source TEXT, url TEXT, company TEXT,
            PRIMARY KEY(source,url));
        CREATE TABLE IF NOT EXISTS findings(
            company TEXT, telegram TEXT, page TEXT, source TEXT, decision TEXT,
            reason TEXT, context TEXT,
            UNIQUE(company,telegram,page,source,decision,reason,context));
        CREATE TABLE IF NOT EXISTS blocked(host TEXT PRIMARY KEY, reason TEXT);
    ''')
    version = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    if version and version[0] != '1':
        raise ValueError('Unknown state schema; choose a new data directory')
    db.execute("INSERT OR IGNORE INTO meta VALUES('schema','1')")
    db.commit()
    return db


def add_task(db, kind, url, source, company='', home='', depth=0):
    url = safe_url(url)
    if url:
        db.execute('INSERT OR IGNORE INTO tasks(kind,url,source,company,home,depth) VALUES(?,?,?,?,?,?)',
                   (kind, url, source, company, home, depth))


def count(db, query, args=()):
    return db.execute(query, args).fetchone()[0]


def reserve_agency(db, source, url, company, limit):
    if db.execute('SELECT 1 FROM agencies WHERE source=? AND url=?', (source, url)).fetchone():
        return False
    if count(db, 'SELECT count(*) FROM agencies WHERE source=?', (source,)) >= limit:
        return False
    db.execute('INSERT INTO agencies VALUES(?,?,?)', (source, url, company))
    return True


def add_site(db, url, source, company, limit, home=None, depth=0):
    if count(db, "SELECT count(*) FROM tasks WHERE kind='site' AND source=? AND company=?",
             (source, company)) < limit:
        add_task(db, 'site', url, source, company, home or url, depth)


def save_findings(db, html, page, source, company):
    for item in extract_telegram(html, page):
        db.execute('INSERT OR IGNORE INTO findings VALUES(?,?,?,?,?,?,?)',
                   (company, item['telegram'], page, source, item['decision'],
                    item['reason'], item['context']))


def process(db, transport, task, config):
    final, html = transport.fetch(task['url'])
    source, kind = task['source'], task['kind']
    if kind == 'listing':
        info = discover(html, final, source)
        for card in info.get('cards', []):
            url = safe_url(card['url'], final)
            if url and reserve_agency(db, source, url, card.get('company', ''), config['max_agencies_per_source']):
                add_task(db, 'card', url, source, card.get('company', ''))
        for item in info.get('inline', []):
            name = item['company']
            key = final + '#' + name
            if reserve_agency(db, source, key, name, config['max_agencies_per_source']):
                save_findings(db, item['html'], final, source, name)
                for website in item['websites']:
                    add_site(db, website, source, name, config['max_site_pages'])
        if count(db, 'SELECT count(*) FROM agencies WHERE source=?', (source,)) < config['max_agencies_per_source']:
            for page in info.get('pages', []):
                if count(db, "SELECT count(*) FROM tasks WHERE kind='listing' AND source=?", (source,)) >= config['max_listing_pages']:
                    break
                add_task(db, 'listing', page, source)
        error = '' if info.get('cards') or info.get('inline') or info.get('pages') else 'no_agencies_or_navigation_found'
    elif kind == 'card':
        info = card_info(html, final, source)
        name = info.get('company') or task['company'] or final
        # The adapter isolates the agency contact block when a directory has unrelated site-wide socials.
        save_findings(db, info.get('html', html), final, source, name)
        db.execute('UPDATE agencies SET company=? WHERE source=? AND url=?', (name, source, task['url']))
        for website in info.get('websites', []):
            add_site(db, website, source, name, config['max_site_pages'])
        error = '' if info.get('websites') else 'no_official_website_found'
    else:
        name = task['company']
        save_findings(db, html, final, source, name)
        if task['depth'] < config['max_site_depth']:
            home = final if task['depth'] == 0 else task['home']
            for link in site_links(html, final, home):
                add_site(db, link, source, name, config['max_site_pages'], home, task['depth'] + 1)
        error = ''
    db.execute("UPDATE tasks SET status='done',error=?,final_url=? WHERE id=?", (error, final, task['id']))
    db.commit()


def old_contacts(path):
    if not path:
        return set()
    if not path.is_file():
        raise ValueError(f'Exclude TXT not found: {path}')
    links = set()
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        raw = line.strip()
        # A legacy list may contain @username; removing @ here is normalization of a supplied
        # contact, not inference of a new username from a company's name.
        if raw.startswith('@'):
            raw = 'https://t.me/' + raw[1:]
        elif re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', raw):
            raw = 'https://t.me/' + raw
        value = canonical_telegram(raw)
        if value:
            links.add(value)
    return links


def csv_safe(value):
    value = re.sub(r'[\r\n\t]+', ' ', str(value))
    return "'" + value if value.startswith(('=', '+', '-', '@')) else value


def atomic_csv(path, headers, rows):
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow(headers)
        writer.writerows([[csv_safe(cell) for cell in row] for row in rows])
    os.replace(temp, path)


def export(db, data, excluded, config, selected, reason, request_count):
    findings = [dict(row) for row in db.execute('SELECT * FROM findings ORDER BY company,telegram,page')]
    for finding in findings:
        finding['telegram'] = canonical_telegram(finding['telegram']) or finding['telegram']
    # If an exact username was explicitly called a channel/bot anywhere,
    # never auto-export it as a contact. All contradictory/ambiguous evidence stays reviewable.
    negative = {f['telegram'] for f in findings if f['decision'] == 'reject'}
    positive = {f['telegram'] for f in findings if f['decision'] == 'contact'}
    accepted = {f['telegram'] for f in findings
                if f['decision'] == 'contact' and f['telegram'] not in negative
                and canonical_telegram(f['telegram']) and f['telegram'] not in excluded}
    contacts_by_link = {}
    for f in findings:
        if f['decision'] == 'contact' and f['telegram'] in accepted:
            contacts_by_link.setdefault(f['telegram'], (f['company'], f['telegram'], f['page']))
    contacts = [contacts_by_link[link] for link in sorted(contacts_by_link)]
    reviews = {}
    for f in findings:
        if f['telegram'] in excluded or f['telegram'] in accepted:
            continue
        if f['telegram'] in negative and f['telegram'] not in positive:
            continue  # Known channel/bot, with no contradictory contact invitation.
        if f['decision'] == 'review' or (f['decision'] == 'contact' and f['telegram'] in negative):
            why = f['reason'] if f['telegram'] not in negative else 'Противоречивое назначение: найдена подпись канала/бота'
            reviews.setdefault(f['telegram'], (f['company'], f['telegram'], f['page'], why, f['context']))
    links = sorted({row[1] for row in contacts})
    tmp = data / 'telegram.txt.tmp'
    tmp.write_text(''.join(link + '\n' for link in links), encoding='utf-8')
    os.replace(tmp, data / 'telegram.txt')
    atomic_csv(data / 'contacts.csv', ['company', 'telegram', 'source_page'], contacts)
    atomic_csv(data / 'review.csv', ['company', 'telegram', 'source_page', 'reason', 'context'],
               [reviews[link] for link in sorted(reviews)])
    sources = {}
    for source in selected:
        tasks = [dict(row) for row in db.execute('SELECT kind,url,status,error,final_url FROM tasks WHERE source=?', (source,))]
        agencies = count(db, 'SELECT count(*) FROM agencies WHERE source=?', (source,))
        sources[source] = dict(agencies=agencies,
                               successful_pages=sum(t['status'] == 'done' for t in tasks),
                               pending_pages=sum(t['status'] == 'pending' for t in tasks), tasks=tasks,
                               limits_reached=dict(agencies=agencies >= config['max_agencies_per_source'],
                                                   listing_pages=sum(t['kind'] == 'listing' for t in tasks) >= config['max_listing_pages']))
    report = dict(time_utc=datetime.now(timezone.utc).isoformat(), stop_reason=reason,
                  requests_this_run=request_count, unique_contacts=len(links), contact_rows=len(contacts),
                  review_rows=len(reviews), excluded_loaded=len(excluded),
                  rejected_occurrences=sum(f['decision'] == 'reject' for f in findings),
                  limits={k: v for k, v in config.items() if k != 'proxy'},
                  proxy_configured=bool(config.get('proxy')), sources=sources,
                  stopped_hosts=[dict(r) for r in db.execute('SELECT * FROM blocked')])
    path = data / 'report.json.tmp'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(path, data / 'report.json')
    return len(links), len(reviews)


def load_config(path):
    config = DEFAULTS.copy()
    if path.is_file():
        custom = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(custom, dict) or set(custom) - set(DEFAULTS):
            raise ValueError('Unknown configuration keys; see config.example.json')
        config.update(custom)
    elif path.name != 'config.json':
        raise ValueError(f'Config not found: {path}')
    for key in ('delay', 'jitter', 'timeout'):
        if isinstance(config[key], bool) or not isinstance(config[key], (int, float)) or not 0 <= config[key] <= 3600:
            raise ValueError(f'Invalid {key}')
    if config['delay'] < 1 or config['timeout'] < 1:
        raise ValueError('delay and timeout must be at least 1 second')
    for key in ('max_requests', 'max_agencies_per_source', 'max_listing_pages', 'max_site_pages', 'max_site_depth'):
        if isinstance(config[key], bool) or not isinstance(config[key], int) or config[key] < 1:
            raise ValueError(f'Invalid {key}')
    if config['proxy'] is not None and not isinstance(config['proxy'], str):
        raise ValueError('proxy must be a URL string or null')
    if not isinstance(config['require_proxy'], bool):
        raise ValueError('require_proxy must be true or false')
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('config.json'))
    parser.add_argument('--sample', action='store_true', help='2 agencies per source, bounded real HTTP sample')
    parser.add_argument('--sources', nargs='+', choices=list(SOURCES), default=list(SOURCES))
    parser.add_argument('--data-dir', type=Path, help='Persistent state and output directory')
    parser.add_argument('--exclude', type=Path, help='Existing UTF-8 TXT: skip these Telegram contacts')
    parser.add_argument('--require-proxy', action='store_true', help='Refuse HTTP unless an explicit proxy is set')
    parser.add_argument('--export-only', action='store_true', help='Export saved findings without HTTP')
    parser.add_argument('--retry-errors', action='store_true', help='Retry failed tasks except persisted stopped hosts')
    args = parser.parse_args(argv)
    data = args.data_dir or Path('results') / ('sample' if args.sample else 'full')
    db = transport = None
    lock = None
    lock_path = data / '.run.lock'
    try:
        config = load_config(args.config)
        config['proxy'] = os.environ.get('AGENCY_PROXY') or config['proxy']
        if (args.require_proxy or config['require_proxy']) and not config['proxy'] and not args.export_only:
            raise ValueError('Proxy required: set AGENCY_PROXY or proxy in config.json')
        if args.sample:
            caps = dict(max_agencies_per_source=2, max_listing_pages=3, max_site_pages=4,
                        max_site_depth=2, max_requests=150)
            config.update({k: min(config[k], v) for k, v in caps.items()})
        excluded = old_contacts(args.exclude)
        data.mkdir(parents=True, exist_ok=True)
        try:
            lock = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(lock, str(os.getpid()).encode('ascii'))
        except FileExistsError:
            raise ValueError('Another run or stale .run.lock. Close other runs before removing that lock file.') from None
        db = connect(data / 'progress.sqlite3')
        mode = 'sample' if args.sample else 'full'
        previous = db.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
        if previous and previous[0] != mode:
            raise ValueError('Sample and full runs must use different data directories')
        db.execute("INSERT OR IGNORE INTO meta VALUES('mode',?)", (mode,))
        if not args.export_only:
            for source in args.sources:
                add_task(db, 'listing', SOURCES[source], source)
            if args.retry_errors:
                db.execute("UPDATE tasks SET status='pending',error='' WHERE status='error'")
        db.commit()
        reason = 'export_only' if args.export_only else 'queue_finished_within_configured_limits'
        if not args.export_only:
            transport = Transport(db, data, config['delay'], config['jitter'], config['timeout'],
                                  config['proxy'], config['max_requests'])
            try:
                while True:
                    marks = ','.join('?' for _ in args.sources)
                    task = db.execute(f"SELECT * FROM tasks WHERE status='pending' AND source IN ({marks}) "
                                      "ORDER BY CASE kind WHEN 'card' THEN 0 WHEN 'site' THEN 1 ELSE 2 END,id LIMIT 1",
                                      args.sources).fetchone()
                    if task is None:
                        break
                    if task['kind'] == 'listing' and count(db, 'SELECT count(*) FROM agencies WHERE source=?', (task['source'],)) >= config['max_agencies_per_source']:
                        db.execute("UPDATE tasks SET status='skipped',error='agency_limit_reached' WHERE id=?", (task['id'],))
                        db.commit()
                        continue
                    print(f"[{task['source']}] {task['kind']}: {task['url']}", flush=True)
                    try:
                        process(db, transport, task, config)
                    except FetchError as exc:
                        db.rollback()
                        db.execute("UPDATE tasks SET status='error',error=? WHERE id=?", (str(exc), task['id']))
                        db.commit()
                        print('  skipped: ' + str(exc), flush=True)
                    except (ValueError, KeyError, TypeError, AttributeError) as exc:
                        db.rollback()
                        error = 'parse_error: ' + type(exc).__name__
                        db.execute("UPDATE tasks SET status='error',error=? WHERE id=?", (error, task['id']))
                        db.commit()
                        print('  skipped: ' + error, flush=True)
            except BudgetStop:
                db.rollback()
                reason = 'request_budget_reached_resume_next_run'
            except KeyboardInterrupt:
                db.rollback()
                reason = 'interrupted_resume_next_run'
        good, review = export(db, data, excluded, config, args.sources, reason, transport.count if transport else 0)
        print(f'Saved: {data.resolve()} | contacts={good}, review={review}; {reason}')
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        # Config/OS errors do not contain request/proxy exception text.
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    finally:
        if transport:
            transport.close()
        if db:
            db.close()
        if lock is not None:
            os.close(lock)
            lock_path.unlink(missing_ok=True)


if __name__ == '__main__':
    raise SystemExit(main())
