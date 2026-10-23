"""Segment buffers must be released without waiting for cyclic GC."""
import asyncio
import gc
import tempfile
import tracemalloc
import unittest
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import proxy
from aiohttp import web


async def no_refresh(self):
    pass


async def prepared_fragment(self, track, fragment, timestamp):
    return fragment


async def fetch_fragment(*args, **kwargs):
    return b'x' * (2 * 1024 * 1024)


async def fake_mux(command):
    Path(command[-1]).write_bytes(bytes(188 * 60))
    return 0, b''


class SegmentMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='shaka-memory-regression-')
        self.addCleanup(self.directory.cleanup)
        self.origin = SimpleNamespace(work_sem=asyncio.Semaphore(4),
                                      process_sem=asyncio.Semaphore(4),
                                      fetch_rel=fetch_fragment)
        self.session = proxy.ChannelSession(
            SimpleNamespace(slug='fixture', keys={}),
            {'hls_dir': self.directory.name, 'ffmpeg': 'unused'}, self.origin)
        self.session.video = SimpleNamespace(
            entries=[(1000, 1000)], timescale=1000, media_tmpl='$Time$',
            rep_id='video', smooth=True, init_data=b'init', codecs='avc1')
        for target, replacement in (('refresh', no_refresh),
                                    ('_smooth_fragment', prepared_fragment)):
            patcher = patch.object(proxy.ChannelSession, target, new=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(proxy, 'run_cmd', new=fake_mux)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_open_channel_does_not_retain_completed_segment_buffers(self):
        enabled = gc.isenabled()
        gc.collect()
        gc.disable()
        tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[0]
            for n in range(1, 33):
                timestamp = n * 1000
                self.session.video.entries = [(timestamp, 1000)]
                await self.session.remux(timestamp)
            retained = tracemalloc.get_traced_memory()[0] - before
            self.assertLess(retained, 4 * 1024 * 1024,
                            'Completed fragments remain referenced while the channel is open')
            self.assertEqual(len(list(self.session.hls_dir.glob('seg_*.ts'))), self.session.window)
            self.assertFalse(list(self.session.hls_dir.glob('.tmp-*')))
        finally:
            tracemalloc.stop()
            if enabled:
                gc.enable()
            gc.collect()

    async def test_closed_session_is_released_without_cyclic_collection(self):
        enabled = gc.isenabled()
        gc.disable()
        try:
            await self.session.remux(1000)
            await self.session.stop()
            reference = weakref.ref(self.session)
            del self.session
            self.assertIsNone(reference(), 'Segment closures retain the closed session')
        finally:
            if enabled:
                gc.enable()
            gc.collect()

    async def test_failed_segment_does_not_retain_the_session(self):
        async def failed_mux(command):
            return 1, b'fixture failure'

        enabled = gc.isenabled()
        gc.disable()
        try:
            with patch.object(proxy, 'run_cmd', new=failed_mux):
                try:
                    await self.session.remux(1000)
                except web.HTTPBadGateway:
                    pass
                else:
                    self.fail('Expected the simulated mux failure')
            await self.session.stop()
            reference = weakref.ref(self.session)
            del self.session
            self.assertIsNone(reference(), 'Failed segment retains its session')
        finally:
            if enabled:
                gc.enable()
            gc.collect()


class InitCacheMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_cache_bounds_entries_and_keeps_recently_used_data(self):
        origin = proxy.DashOrigin({}, SimpleNamespace(get=lambda: ''))
        for n in range(proxy.INIT_CACHE_MAX_ENTRIES):
            origin._remember_init('fixture', str(n), bytes(256))
        ch = SimpleNamespace(slug='fixture')
        self.assertEqual(await origin.fetch_rel(ch, '0', use_init_cache=True), bytes(256))
        origin._remember_init('fixture', 'new', bytes(256))
        self.assertEqual(len(origin.init_cache), proxy.INIT_CACHE_MAX_ENTRIES)
        self.assertIn(('fixture', '0'), origin.init_cache)
        self.assertNotIn(('fixture', '1'), origin.init_cache)

    async def test_init_cache_bounds_bytes_and_releases_closed_channels(self):
        origin = proxy.DashOrigin({}, SimpleNamespace(get=lambda: ''))
        for n in range(12):
            origin._remember_init('fixture', str(n), bytes(proxy.INIT_BODY_MAX))
        self.assertLessEqual(sum(map(len, origin.init_cache.values())), proxy.INIT_CACHE_MAX_BYTES)
        origin._remember_init('other', 'init', bytes(256))
        origin.cdn_base['fixture'] = 'https://example.test/'
        origin.forget_channel('fixture')
        self.assertEqual(list(origin.init_cache), [('other', 'init')])
        self.assertNotIn('fixture', origin.cdn_base)

    async def test_dropping_last_session_clears_media_and_origin_state(self):
        with tempfile.TemporaryDirectory(prefix='shaka-drop-memory-') as directory:
            cfg = dict(hls_dir=directory, source_m3u=str(Path(directory) / 'unused.m3u'),
                       token_file=str(Path(directory) / 'unused.json'))
            state = proxy.AppState(cfg)
            session = proxy.ChannelSession(SimpleNamespace(slug='fixture'), cfg, state.origin)
            session.mpd_body = bytes(1024)
            session._published[1] = 1000
            state.sessions['fixture'] = session
            state.origin._remember_init('fixture', 'init', bytes(256))
            await state._drop('fixture')
            self.assertFalse(state.sessions)
            self.assertFalse(state.origin.init_cache)
            self.assertEqual(session.mpd_body, b'')
            self.assertFalse(session._published)
            self.assertFalse(session.hls_dir.exists())


if __name__ == '__main__':
    unittest.main()
