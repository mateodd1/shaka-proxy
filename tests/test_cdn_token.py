"""Disabling the optional CDN token disables its header, reads and status card."""
import json
import tempfile
import time
import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import proxy


class CardsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.token = None
        self.cards = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'div' and attrs.get('id') == 'token-card':
            self.token = attrs
        if tag == 'div' and 'cards' in attrs.get('class', '').split():
            self.cards = attrs


class CDNTokenTests(unittest.TestCase):
    def make_origin(self, **options):
        cfg = dict(default_ua='fixture', origin='', referer='', **options)
        tokens = SimpleNamespace(get=Mock(return_value='stored-token'))
        return proxy.DashOrigin(cfg, tokens), tokens

    def make_state(self, **options):
        return SimpleNamespace(
            cfg=options, sessions={}, playlist=SimpleNamespace(channels=[]),
            tokens=SimpleNamespace(get=Mock(return_value='stored-token'), exp=time.time()+3600))

    def test_enabled_by_default_and_global_token_overrides_playlist(self):
        origin, tokens = self.make_origin()
        ch = proxy.Channel('fixture', 'Fixture', '', 'https://example.test/',
                           extra_headers={'x-tcdn-token': 'playlist-token'})
        self.assertEqual(origin.headers_for(ch)['x-tcdn-token'], 'stored-token')
        tokens.get.assert_called_once()
        data = proxy.session_snapshot(self.make_state())
        self.assertTrue(data['cdn_token_enabled'])
        self.assertTrue(data['token_ok'])

    def test_enabled_can_use_playlist_token_without_global_token(self):
        origin, tokens = self.make_origin(cdn_token_enabled=True)
        tokens.get.return_value = ''
        ch = proxy.Channel('fixture', 'Fixture', '', 'https://example.test/',
                           extra_headers={'x-tcdn-token': 'playlist-token'})
        self.assertEqual(origin.headers_for(ch)['x-tcdn-token'], 'playlist-token')

    def test_disabled_skips_store_and_all_token_header_case_variants(self):
        origin, tokens = self.make_origin(cdn_token_enabled=False)
        tokens.get.side_effect = AssertionError('Disabled token store was read')
        ch = proxy.Channel('fixture', 'Fixture', '', 'https://example.test/',
                           extra_headers={'x-tcdn-token': 'lower', 'X-TCDN-Token': 'upper',
                                          'X-Other': 'preserved'})
        headers = origin.headers_for(ch)
        self.assertNotIn('x-tcdn-token', {key.lower() for key in headers})
        self.assertEqual(headers['X-Other'], 'preserved')
        tokens.get.assert_not_called()

    def test_disabled_snapshot_does_not_read_token_or_report_stale_expiry(self):
        state = self.make_state(cdn_token_enabled=False)
        state.tokens.get.side_effect = AssertionError('Disabled token store was read')
        data = proxy.session_snapshot(state)
        self.assertFalse(data['cdn_token_enabled'])
        self.assertFalse(data['token_ok'])
        self.assertIsNone(data['token_left_s'])
        self.assertNotIn('stored-token', json.dumps(data))

    def test_card_is_hidden_on_first_render_only_when_disabled(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                data = proxy.session_snapshot(self.make_state(cdn_token_enabled=enabled))
                page = proxy.render_status_page(data)
                parser = CardsParser()
                parser.feed(page)
                self.assertEqual('hidden' in parser.token, not enabled)
                self.assertEqual('no-token' in parser.cards['class'].split(), not enabled)

    def test_missing_status_flag_preserves_existing_card(self):
        parser = CardsParser()
        parser.feed(proxy.render_status_page({'live': []}))
        self.assertNotIn('hidden', parser.token)

    def test_config_requires_a_boolean_and_defaults_to_enabled(self):
        with tempfile.TemporaryDirectory(prefix='shaka-token-config-') as directory:
            path = Path(directory) / 'config.json'
            with patch.object(proxy, 'CONFIG_PATH', path):
                for data, expected in (({}, True), ({'cdn_token_enabled': True}, True),
                                       ({'cdn_token_enabled': False}, False)):
                    path.write_text(json.dumps(data))
                    self.assertIs(proxy.load_config()['cdn_token_enabled'], expected)
                for invalid in ('false', 0, None):
                    path.write_text(json.dumps({'cdn_token_enabled': invalid}))
                    with self.assertRaises(ValueError):
                        proxy.load_config()


if __name__ == '__main__':
    unittest.main()
