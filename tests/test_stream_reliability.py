"""No-network regressions for continuous output and producer lifecycle."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from unittest.mock import Mock

import aiohttp
from aiohttp import web

import proxy


def packet(pid=256, cc=0, control=1):
    head = bytes((0x47, (pid >> 8) & 31, pid & 255, (control << 4) | cc))
    return head + (bytes((183, 0)) + bytes(182) if control == 2 else bytes(184))


class ContinuityTests(unittest.TestCase):
    def test_resets_across_segments_are_continuous_and_input_is_unchanged(self):
        continuity = proxy.TSContinuity()
        source = packet(cc=14) + packet(cc=15)
        self.assertEqual(continuity.rewrite(source), source)
        result = continuity.rewrite(packet(cc=0) + packet(cc=0))
        self.assertEqual([result[3] & 15, result[191] & 15], [0, 1])
        self.assertEqual(source, packet(cc=14) + packet(cc=15))

    def test_counters_are_per_pid_and_connection(self):
        first, second = proxy.TSContinuity(), proxy.TSContinuity()
        first.rewrite(packet(256, 5) + packet(257, 12))
        self.assertEqual(first.rewrite(packet(256))[3] & 15, 6)
        self.assertEqual(first.rewrite(packet(257))[3] & 15, 13)
        self.assertEqual(second.rewrite(packet(256))[3] & 15, 0)

    def test_adaptation_only_does_not_increment_and_null_is_untouched(self):
        continuity = proxy.TSContinuity()
        continuity.rewrite(packet(cc=7))
        self.assertEqual(continuity.rewrite(packet(control=2))[3] & 15, 7)
        self.assertEqual(continuity.rewrite(packet())[3] & 15, 8)
        null = packet(8191, 9)
        self.assertEqual(continuity.rewrite(null + null), null + null)

    def test_rejects_incomplete_or_unsynchronized_packets(self):
        for data in (bytes(188), packet()[:-1]):
            with self.assertRaises(ValueError):
                proxy.TSContinuity().rewrite(data)


class ProducerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='shaka-reliability-')
        self.addCleanup(self.tmp.cleanup)
        self.origin = SimpleNamespace(recycle=AsyncMock())
        self.sess = proxy.ChannelSession(
            SimpleNamespace(slug='test', keys={}), {'hls_dir': self.tmp.name}, self.origin
        )
        self.sess.video = SimpleNamespace(entries=[(n * 150, 150) for n in range(1, 9)], timescale=25)
        self.entries = [(n * 150, 150) for n in range(1, 7)]

    def test_joins_latest_then_recovers_in_order_after_edge_advances(self):
        self.assertEqual(self.sess.next_pending(self.entries), (900, 150))
        self.sess._next_t = 1050
        self.entries.extend([(1050, 150), (1200, 150)])
        self.assertEqual(self.sess.next_pending(self.entries), (1050, 150))

    def test_failed_segment_is_retried_before_newer_edge(self):
        self.sess._next_t = 900
        self.sess._fail_t[900] = 1
        self.entries.append((1050, 150))
        self.assertEqual(self.sess.next_pending(self.entries), (900, 150))

    def test_ready_and_explicitly_skipped_segments_advance_cursor(self):
        self.sess._next_t = 600
        (self.sess.hls_dir / 'seg_600.ts').write_bytes(bytes(41000))
        self.sess._skip_t.add(750)
        self.assertEqual(self.sess.next_pending(self.entries), (900, 150))

    def test_expired_segment_recovers_at_oldest_available_not_latest(self):
        self.sess._next_t = 0
        with self.assertLogs(proxy.log, level='WARNING'):
            self.assertEqual(self.sess.next_pending(self.entries), (150, 150))

    def test_completed_segment_survives_manifest_regression_and_pruning(self):
        # This segment is no longer in closed_entries after an older MPD
        # response. It has already been produced and must not disappear.
        self.sess._published = {900: 150, 1050: 150}
        for t in self.sess._published:
            (self.sess.hls_dir / f'seg_{t}.ts').write_bytes(bytes(41000))
        self.sess.video.entries = self.entries[:4]
        self.sess._prune_cache()
        self.assertEqual(self.sess.ready_entries(pin=True), [(900, 150), (1050, 150)])

    def test_cache_retains_older_ready_segments_across_a_gap(self):
        self.sess._published = {150: 150, 450: 150}
        for t in self.sess._published:
            (self.sess.hls_dir / f'seg_{t}.ts').write_bytes(bytes(41000))
        self.assertEqual(self.sess.ready_entries(), [(150, 150), (450, 150)])

    def test_published_cache_is_bounded(self):
        self.sess._published = {t: 150 for t in range(150, 1501, 150)}
        for t in self.sess._published:
            (self.sess.hls_dir / f'seg_{t}.ts').write_bytes(bytes(41000))
        self.sess._prune_cache()
        self.assertEqual(len(self.sess._published), self.sess.window)
        self.assertEqual(len(list(self.sess.hls_dir.glob('seg_*.ts'))), self.sess.window)

    async def test_stop_awaits_inflight_refresh_cleanup(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def refresh():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        with patch.object(self.sess, 'refresh', side_effect=refresh):
            self.sess.start_producer()
            await asyncio.wait_for(entered.wait(), 2)
            await self.sess.stop()
        self.assertTrue(cleaned.is_set())
        self.assertTrue(self.sess.task.done())

    async def test_stop_awaits_inflight_remux_cleanup(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def remux(t):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        with patch.object(self.sess, 'refresh', new=AsyncMock()), patch.object(self.sess, 'remux', side_effect=remux):
            self.sess.start_producer()
            await asyncio.wait_for(entered.wait(), 2)
            await self.sess.stop()
        self.assertTrue(cleaned.is_set())
        self.assertTrue(self.sess.task.done())

    async def test_cancelling_lock_waiter_does_not_delete_owner_files(self):
        entered = asyncio.Event()
        path = self.sess.hls_dir / '.tmp-900'
        async def owner():
            async with self.sess._segment_work(900):
                path.mkdir()
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(owner())
        await entered.wait()
        async def waiter():
            async with self.sess._segment_work(900):
                pass
        waiting = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        self.assertTrue(path.exists())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(path.exists())

    async def test_stop_also_cancels_http_remux_workers(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def remux(t):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        with patch.object(self.sess, '_remux', side_effect=remux):
            task = asyncio.create_task(self.sess.remux(900))
            await entered.wait()
            await self.sess.stop()
        self.assertTrue(task.done())
        self.assertTrue(cleaned.is_set())
        self.assertFalse(self.sess._workers)

    async def test_failed_download_cancels_and_awaits_sibling(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def failure():
            await entered.wait()
            raise OSError('test download failure')
        async def sibling():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        with self.assertRaises(OSError):
            await proxy.gather_owned(failure(), sibling())
        self.assertTrue(cleaned.is_set())

    async def test_http_shutdown_closes_idle_stream_without_hanging(self):
        self.sess.start_producer = Mock()
        self.sess.ready_entries = Mock(return_value=[])
        state = SimpleNamespace(sessions={'test': self.sess}, get_session=AsyncMock(return_value=self.sess))
        app = web.Application()
        app['state'] = state
        app['reaper'] = asyncio.create_task(asyncio.Event().wait())
        app.router.add_get('/live/{slug}/stream.ts', proxy.handle_stream)
        app.on_shutdown.append(proxy.on_shutdown)
        runner = web.AppRunner(app, shutdown_timeout=3)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as client:
                response = await client.get(f'http://127.0.0.1:{port}/live/test/stream.ts')
                self.assertEqual(self.sess.viewers, 1)
                await asyncio.wait_for(runner.cleanup(), 2)
                self.assertEqual(await response.read(), b'')
                self.assertEqual(self.sess.viewers, 0)
                self.assertTrue(app['reaper'].done())
        finally:
            await runner.cleanup()


if __name__ == '__main__':
    unittest.main()
