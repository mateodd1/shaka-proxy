"""EPG must ignore XMLTV channels that are absent from the playlist."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import proxy


class EPGTests(unittest.IsolatedAsyncioTestCase):
    async def test_unmatched_channels_do_not_abort_programme_loading(self):
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=1)).strftime('%Y%m%d%H%M%S %z')
        stop = (now + timedelta(hours=1)).strftime('%Y%m%d%H%M%S %z')
        xml = f'''<tv>
        <channel id="unknown"><display-name>Not in playlist</display-name></channel>
        <channel id="known"><display-name>Known Channel</display-name></channel>
        <programme channel="unknown" start="{start}" stop="{stop}"><title>Ignore me</title></programme>
        <programme channel="known" start="{start}" stop="{stop}"><title>Programme now</title></programme>
        </tv>'''.encode()
        channel = SimpleNamespace(slug='known-channel', tvg_id='known', name='Known Channel')
        playlist = SimpleNamespace(channels=[channel], epg_url=lambda: 'https://example.test/guide.xml')
        epg = proxy.EPG({'default_ua': 'test'}, playlist)
        response = SimpleNamespace(status=200, read=AsyncMock(return_value=xml))
        response_context = AsyncMock()
        response_context.__aenter__.return_value = response
        session_context = AsyncMock()
        session_context.__aenter__.return_value = SimpleNamespace(get=Mock(return_value=response_context))
        with patch.object(proxy.aiohttp, 'ClientSession', return_value=session_context):
            await epg._reload()
        self.assertEqual(list(epg.programmes), ['known-channel'])
        self.assertEqual(epg.programmes['known-channel'][0]['title'], 'Programme now')
        self.assertEqual(epg.error, '')


if __name__ == '__main__':
    unittest.main()
