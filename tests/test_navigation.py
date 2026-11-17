"""Regression tests drawn from issues observed on the real agency sample."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from parser import main, site_links
from network import FetchError
import test_pipeline as fixtures


class NavigationTests(unittest.TestCase):
    def test_information_pages_without_service_and_article_links(self):
        html = '''<a href="/contacts/">Контакты</a>
        <a href="/about/">О компании</a><a href="/partners/">Партнёрам</a>
        <a href="/uslugi/prodvizhenie-v-vk/">ВКонтакте</a>
        <a href="/about/blog/case">Кейс</a>
        <a href="/about/https://agency.example/blog/case">О компании</a>
        <a href="/about/?back_url_admin=/bitrix/admin/">О компании</a>
        <a href="https://other.example/contacts/">Контакты</a>'''
        self.assertEqual(site_links(html, 'https://agency.example/', 'https://agency.example/'),
                         ['https://agency.example/contacts/', 'https://agency.example/about/',
                          'https://agency.example/partners/'])

    def test_proxy_required_fails_before_transport_creation(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {}, clear=True), \
                patch('parser.Transport') as transport, patch('builtins.print'):
            result = main(['--require-proxy', '--data-dir', folder])
            self.assertEqual(result, 1)
            transport.assert_not_called()

    def test_export_only_does_not_construct_transport(self):
        with tempfile.TemporaryDirectory() as folder, patch('parser.Transport') as transport, patch('builtins.print'):
            self.assertEqual(main(['--export-only', '--data-dir', folder]), 0)
            transport.assert_not_called()
            self.assertTrue((Path(folder) / 'contacts.csv').is_file())


class RobotsCanonicalRedirectTests(unittest.TestCase):
    setUp = fixtures.TransportTests.setUp
    transport = fixtures.TransportTests.transport
    def test_https_upgrade_of_robots_is_followed_and_rules_retained(self):
        t, session = self.transport([
            ('http://agency.example/robots.txt', fixtures.Response(301, headers={'Location': 'https://www.agency.example/robots.txt'})),
            ('https://www.agency.example/robots.txt', fixtures.robots('User-agent: *\nDisallow: /private\n')),
        ])
        with self.assertRaisesRegex(FetchError, 'robots_disallowed'):
            t.fetch('http://agency.example/private/contact')
        self.assertEqual(len(session.calls), 2)


if __name__ == '__main__':
    unittest.main()
