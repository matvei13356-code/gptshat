import unittest

from telegram_rules import canonical_telegram, extract_telegram


class TelegramRulesTests(unittest.TestCase):
    def extract(self, html):
        return extract_telegram(html, "https://agency.example/contact/")

    def test_normalization(self):
        for raw in ("https://t.me/Some_User/", "http://telegram.me/Some_User?text=hello", "//telegram.dog/Some_User", "tg://resolve?domain=Some_User"):
            self.assertEqual(canonical_telegram(raw), "https://t.me/some_user")

    def test_no_guessed_or_spoofed_usernames(self):
        for raw in ("@username", "username", "https://t.me.evil.test/username", "https://evil.test/t.me/username", "https://t.me/a", "https://t.me/пользователь", "https://t.me/_username"):
            self.assertIsNone(canonical_telegram(raw), raw)
        self.assertEqual(self.extract("<p>Напишите @username</p>"), [])

    def test_generic_icon_and_contacts_heading_are_ambiguous(self):
        rows = self.extract('<h1>Контакты</h1><a href="https://t.me/agency_user">Telegram</a>')
        self.assertEqual(rows[0]["decision"], "review")

    def test_explicit_local_invitation(self):
        rows = self.extract('<div>Напишите нам: <a href="https://t.me/agency_user"><img alt="Telegram"></a></div>')
        self.assertEqual(rows[0]["decision"], "contact")
        self.assertEqual(rows[0]["telegram"], "https://t.me/agency_user")

    def test_explicit_telegram_for_contact_labels(self):
        labels = ('telegram для связи @onlydigitalagency', 'telegram for contact @onlydigitalagency',
                  'Для связи в Telegram', 'For contact: Telegram')
        for label in labels:
            with self.subTest(label=label):
                rows = self.extract(f'<a href="https://t.me/onlydigitalagency">{label}</a>')
                self.assertEqual(rows[0]["decision"], "contact")
        rows = self.extract('<a href="https://t.me/onlydigitalagency">Telegram для связи, наш канал</a>')
        self.assertEqual(rows[0]["decision"], "reject")

    def test_channel_overrides_contact_words(self):
        rows = self.extract('<a href="https://t.me/agency_news">Напишите нам в канал, подпишитесь</a>')
        self.assertEqual(rows[0]["decision"], "reject")

    def test_subscribe_label_without_channel_word(self):
        rows = self.extract('<a href="https://t.me/agency_news">Подпишитесь</a>')
        self.assertEqual(rows[0]["decision"], "reject")

    def test_nearby_news_link_does_not_contaminate_contact_link(self):
        rows = self.extract('<footer><div><a href="https://t.me/agency_news">Наш канал</a></div><div><a href="https://t.me/agency_user">Написать нам</a></div></footer>')
        self.assertEqual([r["decision"] for r in rows], ["reject", "contact"])

    def test_parent_channel_warning_overrides_write_button(self):
        rows = self.extract('<div>Наш канал <a href="https://t.me/agency_news">Написать</a></div>')
        self.assertEqual(rows[0]["decision"], "reject")

    def test_published_analytics_url_without_redirect_request(self):
        rows = self.extract('<div>Напишите нам: <a href="/redirect/?url=encoded" data-analytics-id="https://t.me/Web_4px">Telegram</a></div>')
        self.assertEqual(rows[0]["decision"], "contact")
        self.assertEqual(rows[0]["telegram"], "https://t.me/web_4px")
        self.assertEqual(self.extract('<a href="/redirect/?url=encoded" data-analytics-id="web_4px">Telegram</a>'), [])

    def test_analytics_without_local_invitation_is_review(self):
        rows = self.extract('<a href="/redirect/?url=encoded" data-analytics-id="https://t.me/web_4px">Telegram</a>')
        self.assertEqual(rows[0]["decision"], "review")

    def test_sibling_link_cannot_borrow_invitation(self):
        rows = self.extract('<div>Написать нам <a href="https://t.me/agency_one">Telegram</a><a href="https://t.me/agency_two">Telegram</a></div>')
        self.assertEqual([r["decision"] for r in rows], ["review", "review"])

    def test_disallowed_links(self):
        links = ("https://t.me/agencybot", "https://t.me/AgencyBot", "https://t.me/agency_user/123", "https://t.me/s/agency_user", "https://t.me/share/url?url=example.com", "https://t.me/agency_user?start=hello", "tg://resolve?domain=agency_user&startgroup=hello", "tg://resolve?domain=agency_user&post=123", "tg://msg_url?url=example.com")
        rows = self.extract("".join(f'<a href="{x}">Написать</a>' for x in links))
        self.assertTrue(all(r["decision"] == "reject" for r in rows))
        self.assertTrue(all(canonical_telegram(x) is None for x in links))

    def test_invite_business_and_phone_need_review(self):
        links = ("https://t.me/+abcdef", "https://t.me/joinchat/abcdef", "https://t.me/m/businesslink", "tg://resolve?phone=79000000000")
        rows = self.extract("".join(f'<a href="{x}">Написать</a>' for x in links))
        self.assertTrue(all(r["decision"] == "review" for r in rows))

    def test_post_deep_link_query_variants(self):
        for raw in ("tg://resolve?domain=agency_user&post=123", "tg://resolve?domain=agency_user&comment=123", "tg://resolve?domain=agency_user&thread=123", "tg://resolve?domain=agency_user&story=123", "https://t.me/agency_user?post=123", "https://t.me/agency_user?story=123", "https://t.me/agency_user?boost"):
            with self.subTest(raw=raw):
                self.assertIsNone(canonical_telegram(raw))
                self.assertEqual(self.extract(f'<a href="{raw}">Написать</a>')[0]["decision"], "reject")

    def test_other_social_services_ignored(self):
        self.assertEqual(self.extract('<a href="https://wa.me/70000000">Написать</a><a href="https://telegram.org">Telegram</a>'), [])

    def test_visible_url_is_not_a_guessed_username(self):
        rows = self.extract('<p>Напишите нам: https://t.me/agency_user.</p>')
        self.assertEqual(rows[0]["decision"], "contact")
        self.assertEqual(rows[0]["telegram"], "https://t.me/agency_user")
        self.assertEqual(self.extract('<script>"https://t.me/agency_user"</script>'), [])

    def test_hidden_metadata_and_scripts_are_not_visible_urls(self):
        for html in ('<script>Написать https://t.me/agency_user</script>',
                     '<script type="application/ld+json">{"url": "https://t.me/agency_user"}</script>',
                     '<style>/* https://t.me/agency_user */</style>',
                     '<!-- Написать https://t.me/agency_user -->',
                     '<template>Написать https://t.me/agency_user</template>'):
            with self.subTest(html=html):
                self.assertEqual(self.extract(html), [])

    def test_visible_hostname_suffix_does_not_create_published_link(self):
        self.assertEqual(self.extract('<p>Написать: https://foo.t.me/agency_user</p>'), [])

    def test_accessibility_label_and_duplicate(self):
        html = '<a href="https://t.me/agency_user" aria-label="Связаться в Telegram"></a>'
        rows = self.extract(html + html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision"], "contact")


if __name__ == "__main__":
    unittest.main()
