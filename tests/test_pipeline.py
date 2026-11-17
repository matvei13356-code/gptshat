"""Offline checks for persistence, exports and the bounded HTTP transport."""
from __future__ import annotations

import codecs
import csv
import importlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from network import BudgetStop, FetchError, Transport


class Response:
    def __init__(self, status=200, body='', headers=None):
        self.status_code = status
        self.body = body.encode('utf-8')
        self.headers = headers or {'Content-Type': 'text/html; charset=utf-8'}
        self.encoding = 'utf-8'

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, size):
        yield self.body


class Session:
    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []
        self.headers = {}
        self.trust_env = True
        self.proxies = {}
        self.verify = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.script:
            raise AssertionError('Unexpected HTTP request: ' + url)
        expected, result = self.script.pop(0)
        if url != expected:
            raise AssertionError(f'Expected {expected}, got {url}')
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        pass


def robots(text='User-agent: *\nAllow: /\n'):
    return Response(body=text, headers={'Content-Type': 'text/plain'})


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.db = sqlite3.connect(self.data / 'progress.sqlite3')
        self.db.execute('CREATE TABLE blocked(host TEXT PRIMARY KEY, reason TEXT)')
        self.sleep = patch('network.time.sleep').start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.db.close)

    def transport(self, script=(), **options):
        session = Session(script)
        with patch('network.requests.Session', return_value=session):
            transport = Transport(self.db, self.data, delay=1, jitter=0, **options)
        return transport, session

    def test_explicit_proxy_cannot_fall_back_or_use_no_proxy(self):
        secret_proxy = 'http://user:secret@proxy.example:8080'
        failure = requests.exceptions.ProxyError('password=secret')
        t, session = self.transport([('https://agency.example/robots.txt', failure)], proxy=secret_proxy)
        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies, {'http': secret_proxy, 'https': secret_proxy})
        self.assertTrue(session.verify)
        with self.assertRaises(FetchError):
            t.fetch('https://agency.example/contact')
        self.assertEqual(len(session.calls), 1)
        with self.assertRaises(FetchError):
            t.fetch('https://agency.example/about')
        self.assertEqual(len(session.calls), 1)
        log = (self.data / 'requests.jsonl').read_text(encoding='utf-8')
        self.assertNotIn('secret', log)
        self.assertNotIn('user:', log)

    def test_403_stops_host_and_persists_across_transport_restart(self):
        t, session = self.transport([
            ('https://agency.example/robots.txt', robots()),
            ('https://agency.example/', Response(403)),
        ])
        with self.assertRaises(FetchError):
            t.fetch('https://agency.example/')
        self.assertEqual(t.count, 2)
        restarted, new_session = self.transport()
        with self.assertRaises(FetchError):
            restarted.fetch('https://agency.example/contact')
        self.assertEqual(new_session.calls, [])

    def test_robots_denial_never_fetches_disallowed_page(self):
        t, session = self.transport([
            ('https://agency.example/robots.txt', robots('User-agent: *\nDisallow: /private\n')),
        ])
        with self.assertRaisesRegex(FetchError, 'robots_disallowed'):
            t.fetch('https://agency.example/private/contact')
        self.assertEqual(len(session.calls), 1)

    def test_robots_wildcard_query_rule_blocks_pagination(self):
        t, session = self.transport([
            ('https://agency.example/robots.txt', robots('User-agent: *\nDisallow: /*?page=*\n')),
        ])
        with self.assertRaisesRegex(FetchError, 'robots_disallowed'):
            t.fetch('https://agency.example/contractors/?page=2')
        self.assertEqual(len(session.calls), 1)

    def test_page_redirect_checks_destination_robots(self):
        t, session = self.transport([
            ('https://agency.example/robots.txt', robots()),
            ('https://agency.example/', Response(302, headers={'Location': 'https://other.example/contact'})),
            ('https://other.example/robots.txt', robots('User-agent: *\nDisallow: /\n')),
        ])
        with self.assertRaisesRegex(FetchError, 'robots_disallowed'):
            t.fetch('https://agency.example/')
        self.assertEqual(len(session.calls), 3)
        self.assertTrue(all(call[1]['allow_redirects'] is False for call in session.calls))

    def test_same_origin_redirect_cannot_bypass_path_disallow(self):
        t, session = self.transport([
            ('https://agency.example/robots.txt', robots('User-agent: *\nDisallow: /private\n')),
            ('https://agency.example/', Response(302, headers={'Location': '/private/contact'})),
        ])
        with self.assertRaisesRegex(FetchError, 'robots_disallowed'):
            t.fetch('https://agency.example/')
        self.assertEqual(len(session.calls), 2)

    def test_cross_origin_robots_redirect_fails_closed(self):
        t, session = self.transport([
            ('https://agency.example/robots.txt', Response(302, headers={'Location': 'https://other.example/robots.txt'})),
        ])
        with self.assertRaisesRegex(FetchError, 'robots_cross_origin_redirect'):
            t.fetch('https://agency.example/contact')
        self.assertEqual(len(session.calls), 1)

    def test_missing_robots_is_treated_as_no_rules(self):
        t, session = self.transport([
            ('https://agency.example/robots.txt', Response(404)),
            ('https://agency.example/', Response(body='<h1>Agency</h1>')),
        ])
        self.assertEqual(t.fetch('https://agency.example/')[0], 'https://agency.example/')
        self.assertEqual(len(session.calls), 2)

    def test_budget_includes_robots_and_keeps_later_page_unrequested(self):
        t, session = self.transport([
            ('https://agency.example/robots.txt', robots()),
        ], max_requests=1)
        with self.assertRaises(BudgetStop):
            t.fetch('https://agency.example/')
        self.assertEqual(t.count, 1)
        self.assertEqual(len(session.calls), 1)

    def test_captcha_stops_host_without_challenge_requests(self):
        t, session = self.transport([
            ('https://agency.example/robots.txt', robots()),
            ('https://agency.example/', Response(body='<title>CAPTCHA</title><a href="/challenge">Continue</a>')),
        ])
        with self.assertRaisesRegex(FetchError, 'challenge_or_login'):
            t.fetch('https://agency.example/')
        with self.assertRaises(FetchError):
            t.fetch('https://agency.example/challenge')
        self.assertEqual(len(session.calls), 2)


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = importlib.import_module('parser')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.db = self.parser.connect(self.data / 'progress.sqlite3')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: self.db.close())

    def add_finding(self, company, telegram, page, decision, reason='test', context='test'):
        self.db.execute('INSERT OR IGNORE INTO findings VALUES(?,?,?,?,?,?,?)',
                        (company, telegram, page, 'workspace', decision, reason, context))
        self.db.commit()

    def export(self, excluded=None):
        return self.parser.export(self.db, self.data, excluded or set(), self.parser.DEFAULTS,
                                  ['workspace'], 'test', 0)

    def read_csv(self, name):
        with (self.data / name).open(encoding='utf-8-sig', newline='') as f:
            return list(csv.DictReader(f, delimiter=';'))

    def test_deduplicates_global_txt_and_csv_with_first_provenance_and_bom(self):
        for company in ('Агентство А', 'Агентство Б'):
            self.add_finding(company, 'https://t.me/agency_user', 'https://agency.example/contact', 'contact')
        self.add_finding('Агентство А', 'https://t.me/agency_user', 'https://agency.example/contact', 'contact')
        self.add_finding('Агентство А', 'https://t.me/agency_user', 'https://agency.example/about', 'contact')
        self.assertEqual(self.export(), (1, 0))
        self.assertEqual((self.data / 'telegram.txt').read_text(), 'https://t.me/agency_user\n')
        rows = self.read_csv('contacts.csv')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['company'], 'Агентство А')
        self.assertEqual(rows[0]['source_page'], 'https://agency.example/about')
        for name in ('contacts.csv', 'review.csv'):
            self.assertTrue((self.data / name).read_bytes().startswith(codecs.BOM_UTF8))

    def test_legacy_database_alias_and_case_duplicates_are_normalized_at_export(self):
        for company, link in (
            ('A', 'https://telegram.me/Agency_User'),
            ('B', 'https://t.me/agency_user'),
            ('C', 'tg://resolve?domain=AGENCY_USER'),
            ('D', 'https://telegram.dog/Agency_User/'),
        ):
            self.add_finding(company, link, 'https://agency.example/' + company, 'contact')
        self.assertEqual(self.export(), (1, 0))
        self.assertEqual((self.data / 'telegram.txt').read_text(), 'https://t.me/agency_user\n')
        rows = self.read_csv('contacts.csv')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['telegram'], 'https://t.me/agency_user')
        self.assertEqual(rows[0]['company'], 'A')
        self.assertEqual(rows[0]['source_page'], 'https://agency.example/A')

    def test_review_has_one_row_per_link_across_pages_companies_and_contexts(self):
        for company, page, link, context in (
            ('A', 'https://agency.example/about', 'https://t.me/ambiguous_user', 'Telegram'),
            ('A', 'https://agency.example/contact', 'https://t.me/ambiguous_user', 'Social icon'),
            ('B', 'https://second.example/', 'https://telegram.me/Ambiguous_User', 'Footer'),
        ):
            self.add_finding(company, link, page, 'review', 'Unknown purpose', context)
        self.assertEqual(self.export(), (0, 1))
        rows = self.read_csv('review.csv')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['telegram'], 'https://t.me/ambiguous_user')
        self.assertEqual(rows[0]['source_page'], 'https://agency.example/about')

    def test_accepted_contact_is_not_also_exported_for_review(self):
        self.add_finding('A', 'https://telegram.me/Agency_User', 'https://agency.example/', 'review')
        self.add_finding('B', 'https://t.me/agency_user', 'https://agency.example/contact', 'contact')
        self.assertEqual(self.export(), (1, 0))
        self.assertEqual(len(self.read_csv('contacts.csv')), 1)
        self.assertEqual(self.read_csv('review.csv'), [])

    def test_old_txt_normalizes_and_excludes_every_output(self):
        old = self.data / 'old.txt'
        old.write_text('https://telegram.me/Agency_User\n@another_user\nthird_user\n', encoding='utf-8-sig')
        excluded = self.parser.old_contacts(old)
        self.assertEqual(excluded, {'https://t.me/agency_user', 'https://t.me/another_user', 'https://t.me/third_user'})
        self.add_finding('A', 'https://t.me/agency_user', 'https://agency.example/', 'contact')
        self.add_finding('B', 'https://telegram.me/Agency_User', 'https://second.example/', 'contact')
        self.add_finding('A', 'https://t.me/another_user', 'https://agency.example/', 'review')
        self.assertEqual(self.export(excluded), (0, 0))
        self.assertEqual(self.read_csv('contacts.csv'), [])
        self.assertEqual(self.read_csv('review.csv'), [])

    def test_conflicting_channel_label_moves_contact_to_review(self):
        self.add_finding('A', 'https://t.me/agency_user', 'https://agency.example/contact', 'contact')
        self.add_finding('A', 'https://t.me/agency_user', 'https://agency.example/about', 'reject', 'Канал')
        self.assertEqual(self.export(), (0, 1))
        self.assertEqual((self.data / 'telegram.txt').read_text(), '')
        self.assertIn('Противоречивое', self.read_csv('review.csv')[0]['reason'])

    def test_same_username_channel_conflict_applies_across_companies(self):
        self.add_finding('A', 'https://t.me/agency_user', 'https://first.example/contact', 'contact')
        self.add_finding('B', 'https://t.me/agency_user', 'https://second.example/about', 'reject', 'Канал')
        self.assertEqual(self.export(), (0, 1))
        self.assertEqual((self.data / 'telegram.txt').read_text(), '')

    def test_sample_does_not_raise_user_request_and_page_caps(self):
        config = self.data / 'config.json'
        config.write_text(json.dumps({'max_requests': 3, 'max_agencies_per_source': 1,
                                      'max_listing_pages': 1, 'max_site_pages': 1,
                                      'max_site_depth': 1}), encoding='utf-8')
        target = self.data / 'sample'
        with patch('parser.Transport') as factory, patch('builtins.print'):
            factory.return_value.count = 0
            code = self.parser.main(['--sample', '--export-only', '--config', str(config), '--data-dir', str(target)])
        self.assertEqual(code, 0)
        report = json.loads((target / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['limits']['max_requests'], 3)
        self.assertEqual(report['limits']['max_agencies_per_source'], 1)
        self.assertEqual(report['limits']['max_listing_pages'], 1)
        self.assertEqual(report['limits']['max_site_pages'], 1)
        self.assertEqual(report['limits']['max_site_depth'], 1)

    def test_unknown_link_retains_actual_source_page(self):
        self.add_finding('A', 'https://t.me/ambiguous_user', 'https://agency.example/partners', 'review', 'Unclear purpose')
        self.export()
        row = self.read_csv('review.csv')[0]
        self.assertEqual(row['source_page'], 'https://agency.example/partners')
        self.assertEqual(row['telegram'], 'https://t.me/ambiguous_user')

    def test_sqlite_restart_keeps_done_tasks_and_pending_queue(self):
        self.parser.add_task(self.db, 'site', 'https://agency.example/', 'workspace', 'A')
        self.parser.add_task(self.db, 'site', 'https://agency.example/contact', 'workspace', 'A')
        self.db.execute("UPDATE tasks SET status='done' WHERE url='https://agency.example/'")
        self.add_finding('A', 'https://t.me/agency_user', 'https://agency.example/', 'contact')
        self.db.commit()
        self.db.close()
        self.db = self.parser.connect(self.data / 'progress.sqlite3')
        self.parser.add_task(self.db, 'site', 'https://agency.example/', 'workspace', 'A')
        tasks = [dict(row) for row in self.db.execute('SELECT * FROM tasks ORDER BY id')]
        self.assertEqual([task['status'] for task in tasks], ['done', 'pending'])
        self.assertEqual(self.export(), (1, 0))

    def test_csv_formula_like_company_name_is_safe_in_excel(self):
        self.add_finding('=CMD()', 'https://t.me/agency_user', 'https://agency.example/', 'contact')
        self.export()
        self.assertEqual(self.read_csv('contacts.csv')[0]['company'], "'=CMD()")


if __name__ == '__main__':
    unittest.main()
