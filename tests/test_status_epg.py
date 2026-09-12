"""Status polls must not wait for the XMLTV source or launch duplicate downloads."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import proxy


class StatusEPGRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.playlist = SimpleNamespace(channels=[], maybe_reload=Mock())
        self.epg = proxy.EPG({}, self.playlist)

    async def asyncTearDown(self):
        await self.epg.close()

    async def test_repeated_requests_share_one_nonblocking_refresh(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def reload():
            entered.set()
            await release.wait()
            self.epg.programmes = {'test': []}
            self.epg.updated = proxy.time.monotonic()

        with patch.object(self.epg, '_reload', side_effect=reload) as download:
            self.epg.request_refresh()
            await asyncio.wait_for(entered.wait(), 1)
            task = self.epg._refresh_task
            for _ in range(20):
                self.epg.request_refresh()
            self.assertIs(self.epg._refresh_task, task)
            self.assertEqual(download.await_count, 1)
            release.set()
            await task
            self.epg.request_refresh()
            self.assertEqual(download.await_count, 1)

    async def test_failure_keeps_cache_and_limits_retries(self):
        cached = {'test': [{'title': 'Cached programme'}]}
        self.epg.programmes = cached
        self.epg.updated = proxy.time.monotonic() - 1800
        with patch.object(self.epg, '_reload', side_effect=OSError('source unavailable')) as download:
            self.epg.request_refresh()
            with self.assertLogs(proxy.log, level='WARNING'):
                await self.epg._refresh_task
            self.assertIs(self.epg.programmes, cached)
            for _ in range(20):
                self.epg.request_refresh()
            self.assertEqual(download.await_count, 1)
            self.epg._retry_after = 0
            self.epg.request_refresh()
            with self.assertLogs(proxy.log, level='WARNING'):
                await self.epg._refresh_task
            self.assertEqual(download.await_count, 2)

    async def test_epg_page_and_status_share_existing_reload_lock(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def reload():
            entered.set()
            await release.wait()
            self.epg.programmes = {'test': []}
            self.epg.updated = proxy.time.monotonic()

        with patch.object(self.epg, '_reload', side_effect=reload) as download:
            foreground = asyncio.create_task(self.epg.snapshot())
            self.addAsyncCleanup(lambda: asyncio.gather(foreground, return_exceptions=True))
            await asyncio.wait_for(entered.wait(), 1)
            self.epg.request_refresh()
            release.set()
            await asyncio.gather(foreground, self.epg._refresh_task)
            self.assertEqual(download.await_count, 1)

    async def test_close_cancels_pending_download(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def reload():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        with patch.object(self.epg, '_reload', side_effect=reload):
            self.epg.request_refresh()
            await asyncio.wait_for(entered.wait(), 1)
            task = self.epg._refresh_task
            await self.epg.close()
            self.assertTrue(task.cancelled())
            self.assertTrue(cleaned.is_set())

    async def test_both_status_handlers_respond_while_epg_is_loading(self):
        state = SimpleNamespace(
            cfg={'default_ua': 'test', 'referer': ''}, playlist=self.playlist,
            epg=self.epg, sessions={'test': object()},
        )
        request = SimpleNamespace(app={'state': state})
        entered = asyncio.Event()

        async def reload():
            entered.set()
            await asyncio.Event().wait()

        data = {'channels_total': 1, 'open': 1, 'live': [], 'token_ok': True, 'token_left_s': 3600}
        with patch.object(self.epg, '_reload', side_effect=reload), \
                patch.object(proxy, 'session_snapshot', return_value=data):
            for handler in (proxy.handle_status, proxy.handle_status_json):
                response = await asyncio.wait_for(handler(request), 0.5)
                self.assertEqual(response.status, 200)
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(self.epg._refresh_task.done())
            self.assertEqual(json.loads(response.text), data)

    async def test_empty_status_does_not_download_epg(self):
        state = SimpleNamespace(
            cfg={'default_ua': 'test', 'referer': ''}, playlist=self.playlist,
            epg=self.epg, sessions={}, tokens=SimpleNamespace(get=lambda: '', exp=0),
        )
        with patch.object(self.epg, '_reload', new_callable=AsyncMock) as download:
            await proxy.handle_status_json(SimpleNamespace(app={'state': state}))
            download.assert_not_awaited()
            self.assertIsNone(self.epg._refresh_task)


if __name__ == '__main__':
    unittest.main()
