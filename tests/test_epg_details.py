"""Programme metadata and safe rendering for the EPG floating information card."""
import html
import json
import unittest
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import proxy


class PageParser(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.details = []
        self.scripts = 0
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'data-programme' in attrs:
            self.details.append(json.loads(attrs['data-programme']))
        if tag == 'script':
            self.scripts += 1
        if any(key.startswith('on') for key in attrs):
            raise AssertionError('Programme data injected an HTML event handler')


class EPGMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def load(self, fields):
        now = datetime.now(timezone.utc)
        start = (now - timedelta(minutes=10)).strftime('%Y%m%d%H%M%S %z')
        stop = (now + timedelta(minutes=20)).strftime('%Y%m%d%H%M%S %z')
        xml = f'''<tv><channel id="one"><display-name>One</display-name></channel>
        <programme channel="one" start="{start}" stop="{stop}">
        <title>Programme</title>{fields}</programme></tv>'''.encode()
        channel = SimpleNamespace(slug='one', tvg_id='one', name='One')
        playlist = SimpleNamespace(channels=[channel], epg_url=lambda: 'https://example.test/epg.xml')
        epg = proxy.EPG({'default_ua': 'test'}, playlist)
        response_context = AsyncMock()
        response_context.__aenter__.return_value = SimpleNamespace(status=200, read=AsyncMock(return_value=xml))
        session_context = AsyncMock()
        session_context.__aenter__.return_value = SimpleNamespace(get=Mock(return_value=response_context))
        with patch.object(proxy.aiohttp, 'ClientSession', return_value=session_context):
            await epg._reload()
        return epg.programmes['one'][0]

    async def test_keeps_synopsis_line_breaks_subtitle_and_unique_categories(self):
        programme = await self.load('''<sub-title>2026 | 12</sub-title>
        <desc> Primera línea &amp; otra.\nSegunda línea. </desc>
        <category> Cine </category><category>Drama</category>
        <category>Cine</category><category/>''')
        self.assertEqual(programme['description'], 'Primera línea & otra.\nSegunda línea.')
        self.assertEqual(programme['category'], 'Cine · Drama')
        self.assertEqual(programme['subtitle'], '2026 | 12')

    async def test_missing_optional_metadata_is_supported(self):
        programme = await self.load('<desc/><category/>')
        self.assertEqual(programme['description'], '')
        self.assertEqual(programme['category'], '')
        self.assertEqual(programme['title'], 'Programme')
        self.assertEqual(programme['poster'], '')

    async def test_programme_icon_preserves_url_and_resolves_relative_images(self):
        for source, expected in (
            ('https://images.example.test/poster.jpg?a=1&amp;b=2', 'https://images.example.test/poster.jpg?a=1&b=2'),
            ('posters/one.jpg', 'https://example.test/posters/one.jpg'),
        ):
            with self.subTest(source=source):
                programme = await self.load(f'<icon src="{source}"/>')
                self.assertEqual(programme['poster'], expected)

    async def test_empty_and_non_web_icons_are_ignored(self):
        for source in ('', 'javascript:alert(1)', 'file:///etc/passwd', 'data:image/svg+xml,x'):
            with self.subTest(source=source):
                programme = await self.load(f'<icon src="{source}"/>')
                self.assertEqual(programme['poster'], '')


class EPGDetailsPageTests(unittest.TestCase):
    def render(self, **changes):
        start = datetime(2026, 9, 11, 21, 30, tzinfo=timezone.utc)
        programme = {
            'start': start, 'stop': start + timedelta(hours=2),
            'title': 'Una película', 'subtitle': '2026',
        }
        programme.update(changes)
        channel = SimpleNamespace(logo='', name="Canal d'ejemplo", tvg_id='example')
        data = {'error': '', 'rows': [{
            'channel': channel, 'programmes': [programme], 'next': programme,
        }]}
        return proxy.render_epg_page(data)

    def test_missing_synopsis_has_fallback_and_midnight_schedule_includes_both_dates(self):
        parsed = PageParser(self.render())
        self.assertEqual(len(parsed.details), 1)
        details = parsed.details[0]
        self.assertEqual(details['time'], '11/09 · 23:30–12/09 01:30')
        self.assertEqual(details['description'], 'Sin descripción disponible.')
        self.assertEqual(details['category'], '')
        self.assertEqual(details['channel'], "Canal d'ejemplo")
        self.assertEqual(details['poster'], '')

    def test_poster_url_reaches_the_card_without_changing_its_query(self):
        poster = 'https://images.example.test/poster.jpg?one=1&two=2'
        self.assertEqual(PageParser(self.render(poster=poster)).details[0]['poster'], poster)

    def test_untrusted_epg_text_round_trips_without_becoming_markup(self):
        value = '''' " & </script><script>alert(1)</script><img src=x onerror=alert(1)>'''
        page = self.render(title=value, subtitle=value, description=value, category=value)
        parsed = PageParser(page)
        self.assertEqual(parsed.scripts, 1)
        for key in ('title', 'subtitle', 'description', 'category'):
            self.assertEqual(parsed.details[0][key], value)
        self.assertIn(html.escape(value, quote=True), page)


if __name__ == '__main__':
    unittest.main()
