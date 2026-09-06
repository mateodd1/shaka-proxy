"""URL resolution at the existing curl boundary, using fictitious origins."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import proxy


BASE = 'https://origin.example/live/dash/Manifest?start=LIVE&end=END'
CASES = (
    ('segment.m4s', 'https://origin.example/live/dash/segment.m4s'),
    ('../segment.m4s', 'https://origin.example/live/segment.m4s'),
    ('/segment.m4s', 'https://origin.example/segment.m4s'),
    ('?token=xxx', 'https://origin.example/live/dash/Manifest?token=xxx'),
    ('//cdn.example.com/segment.m4s', 'https://cdn.example.com/segment.m4s'),
    ('https://cdn.example.com/segment.m4s', 'https://cdn.example.com/segment.m4s'),
)


class OriginURLTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.origin = proxy.DashOrigin(
            {'default_ua': 'fixture', 'origin': '', 'referer': '', 'cdn_retries': 1},
            SimpleNamespace(get=lambda: ''))
        self.ch = proxy.Channel('fixture', 'Fixture', '', BASE)

    async def test_relative_redirects_for_all_supported_statuses(self):
        for status in (301, 302, 303, 307, 308):
            for location, expected in CASES:
                with self.subTest(status=status, location=location):
                    hop = AsyncMock(side_effect=[
                        (status, b'', BASE, location), (200, b'ok', expected, '')])
                    with patch.object(self.origin, '_curl_hop', hop):
                        result = await self.origin._get(BASE, {})
                    self.assertEqual(hop.await_args_list[1].args[0], expected)
                    self.assertEqual(result, (200, b'ok', expected))

    async def test_init_and_media_resolve_against_final_manifest_url(self):
        for init in (False, True):
            for relative, expected in CASES:
                with self.subTest(init=init, relative=relative):
                    self.origin.init_cache.clear()
                    get = AsyncMock(side_effect=[(200, b'<MPD/>', BASE),
                                                (200, bytes(256), expected)])
                    with patch.object(self.origin, '_get', get):
                        await self.origin.fetch_mpd(self.ch)
                        self.assertEqual(await self.origin.fetch_rel(
                            self.ch, relative, use_init_cache=init), bytes(256))
                    self.assertEqual(get.await_args_list[1].args[0], expected)

    async def test_existing_relative_templates_and_query_slashes(self):
        for manifest, expected in (
            ('https://origin.example/LIVE$CH/index.mpd', 'https://origin.example/LIVE$CH/'),
            ('https://origin.example/LIVE$CH/index.mpd/', 'https://origin.example/LIVE$CH/index.mpd/'),
            ('https://origin.example/LIVE$CH/Manifest?start=LIVE', 'https://origin.example/LIVE$CH/'),
            ('https://origin.example/LIVE$CH/Manifest?next=https://example.com/a/b',
             'https://origin.example/LIVE$CH/'),
        ):
            with self.subTest(manifest=manifest):
                relative = 'video=1000-1700000000.m4s?fixture=a%2Fb'
                get = AsyncMock(side_effect=[(200, b'<MPD/>', manifest),
                                            (200, bytes(256), expected + relative)])
                with patch.object(self.origin, '_get', get):
                    await self.origin.fetch_mpd(self.ch)
                    await self.origin.fetch_rel(self.ch, relative)
                self.assertEqual(get.await_args_list[1].args[0], expected + relative)


if __name__ == '__main__':
    unittest.main()
