import unittest
from pathlib import Path
from sources import SOURCES, card_info, discover

FIXTURES = Path(__file__).parent / 'fixtures'

def fixture(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


class SourceAdapterTests(unittest.TestCase):
    def test_workspace_categories(self):
        d = discover(fixture('workspace_categories.html'), SOURCES['workspace'], 'workspace')
        self.assertIn('https://workspace.ru/web-development/', d['pages'])
        self.assertFalse(d['cards'])

    def test_workspace_cards_and_published_next(self):
        d = discover(fixture('workspace_listing.html'), 'https://workspace.ru/web-development/', 'workspace')
        self.assertIn('https://workspace.ru/contractors/only-com-ru/', [x['url'] for x in d['cards']])
        self.assertIn('https://workspace.ru/web-development/?page=1', d['pages'])
        # Fetch permission for this next URL belongs to the robots-aware transport.

    def test_ruward_cards_and_pagination(self):
        d = discover(fixture('ruward_listing.html'), SOURCES['ruward'], 'ruward')
        self.assertEqual(d['cards'][0]['url'], 'https://ruward.ru/catalog/ingate-ru/')
        self.assertIn('https://ruward.ru/catalog/p2.html', d['pages'])

    def test_yandex_published_slugs_and_next(self):
        d = discover(fixture('yandex_listing.html'), SOURCES['yandex'], 'yandex')
        self.assertEqual(d['cards'][0], {'url': 'https://b2b.yandex.ru/adv/contact/agencies/media-instinct', 'company': 'Media Instinct Group'})
        self.assertEqual(d['pages'], [SOURCES['yandex'] + '?page=2'])

    def test_cms_categories_and_public_api_next(self):
        d = discover(fixture('cms_categories.html'), SOURCES['cmsmagazine'], 'cmsmagazine')
        self.assertIn('https://cmsmagazine.ru/creators/', d['pages'])
        d = discover(fixture('cms_listing.html'), 'https://cmsmagazine.ru/creators/', 'cmsmagazine')
        self.assertEqual(d['cards'][0]['url'], 'https://cmsmagazine.ru/agencies/liqium/')
        self.assertEqual(d['pages'], ['https://api.cmsmagazine.ru/v1/companiesList?company_type_code=creators&page=2'])

    def test_cms_json_next_uses_frontend_host_for_cards(self):
        d = discover(fixture('cms_page2.json'), 'https://api.cmsmagazine.ru/v1/companiesList?company_type_code=creators&page=2', 'cmsmagazine')
        self.assertEqual(d['cards'][0]['url'], 'https://cmsmagazine.ru/agencies/proactivity/')
        self.assertEqual(d['pages'], ['https://api.cmsmagazine.ru/v1/companiesList?company_type_code=creators&page=3'])
        self.assertFalse(d['inline'])

    def test_official_website_selection(self):
        expected = [('workspace', 'workspace_card.html', '4 пикселя', 'https://4px.ru/'),
                    ('ruward', 'ruward_card.html', 'Ingate Group', 'https://ingate.ru/'),
                    ('yandex', 'yandex_card.html', 'Media Instinct Group', 'https://mediainstinctgroup.ru/'),
                    ('cmsmagazine', 'cms_card.html', 'АРТВЕЛЛ', 'http://www.artwell.ru/')]
        for source, name, company, website in expected:
            with self.subTest(source=source):
                info = card_info(fixture(name), SOURCES[source], source)
                self.assertEqual(info['company'], company)
                self.assertEqual(info['websites'], [website])

    def test_workspace_published_telegram_redirect_expanded(self):
        info = card_info(fixture('workspace_card.html'), 'https://workspace.ru/contractors/4px/', 'workspace')
        self.assertIn('href="https://t.me/web_4px"', info['html'])
        self.assertNotIn('/redirect/', info['html'])

    def test_yandex_agency_boundary_excludes_directory_footer(self):
        raw = fixture('yandex_agency_footer.html')
        self.assertIn('Telegram: рынок и аналитика', raw)
        info = card_info(raw, SOURCES['yandex'] + '/media-instinct', 'yandex')
        self.assertIn('Media Instinct Group', info['html'])
        self.assertIn('mediainstinctgroup.ru', info['html'])
        self.assertNotIn('Telegram: рынок и аналитика', info['html'])
        self.assertNotIn('Telegram: новости и обучение', info['html'])
        self.assertNotIn('Telegram: акции и сервисы для бизнеса', info['html'])
        self.assertNotIn('data-testid="Footer"', info['html'])

    def test_yandex_boundary_preserves_agency_link_without_username_blacklist(self):
        raw = '<div data-testid="AgencyHeroBanner"><h1>Agency</h1><div data-testid="ProfileContacts"><a href="https://t.me/public_user">Напишите нам</a></div></div><div data-testid="Footer"><a href="https://t.me/another_user">Напишите нам</a></div>'
        info = card_info(raw, SOURCES['yandex'] + '/agency', 'yandex')
        self.assertIn('https://t.me/public_user', info['html'])
        self.assertNotIn('https://t.me/another_user', info['html'])

    def test_yandex_missing_agency_region_does_not_process_whole_page(self):
        raw = '<h1>Agency</h1><div data-testid="Footer"><a href="https://t.me/public_user">Напишите нам</a></div>'
        info = card_info(raw, SOURCES['yandex'] + '/agency', 'yandex')
        self.assertEqual(info['html'], '')

    def test_invalid_cms_json_is_safe(self):
        self.assertEqual(discover('{ broken', 'https://api.cmsmagazine.ru/v1/companiesList', 'cmsmagazine')['cards'], [])


if __name__ == '__main__':
    unittest.main()
