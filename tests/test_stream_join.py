"""Regression tests for clients joining a shared MPEG-TS channel.

Source: PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
Installed module: .venv/bin/python -m unittest discover -s tests -v
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import proxy


class StreamJoinTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shaka-join-test-")
        self.addCleanup(self.tmp.cleanup)
        self.entries = [(n * 6000, 6000) for n in range(1, 7)]
        self.sess = SimpleNamespace(
            ch=SimpleNamespace(slug="test"),
            hls_dir=Path(self.tmp.name),
            video=SimpleNamespace(timescale=1000),
            stopped=False,
            viewers=1,
            clients={"existing": {"ua": "first player"}},
            start_producer=Mock(),
            touch=Mock(),
            ready_entries=Mock(side_effect=lambda **kw: list(self.entries)),
        )
        self.state = SimpleNamespace(get_session=AsyncMock(return_value=self.sess))
        self.request = SimpleNamespace(
            app={"state": self.state}, match_info={"slug": "test"},
            headers={}, remote="127.0.0.1",
            transport=SimpleNamespace(is_closing=lambda: False),
        )
        self.resp = SimpleNamespace(prepare=AsyncMock(), write_eof=AsyncMock())
        self.sent = []
        self.sent_at = []
        self.clock = 0.0
        self.on_sleep = lambda: None
        for n in range(1, 9):
            # Distinct payloads identify the selected segment, above the size
            # threshold. Codec handling is unrelated to the join cursor.
            (self.sess.hls_dir / f"seg_{n * 6000}.ts").write_bytes(bytes([n]) * 41360)

    async def run_stream(self, on_write):
        async def send(resp, data, duration=0.0, continuity=None):
            self.assertIs(resp, self.resp)
            self.sent.append((data[0], duration))
            self.sent_at.append(self.clock)
            on_write()

        real_sleep = asyncio.sleep
        async def sleep(delay):
            self.clock += delay
            self.on_sleep()
            await real_sleep(0)

        with patch.object(proxy.web, "StreamResponse", return_value=self.resp), \
                patch.object(proxy, "write_ts", side_effect=send), \
                patch.object(proxy, "time", SimpleNamespace(monotonic=lambda: self.clock, time=lambda: 0)), \
                patch.object(proxy.asyncio, "sleep", side_effect=sleep):
            await asyncio.wait_for(proxy.handle_stream(self.request), timeout=2)
        # Joining/leaving must not remove the first viewer or its state.
        self.assertEqual(self.sess.viewers, 1)
        self.assertEqual(self.sess.clients, {"existing": {"ua": "first player"}})

    async def test_late_client_starts_at_latest_not_cache_start(self):
        await self.run_stream(lambda: setattr(self.sess, "stopped", True))
        self.assertEqual(self.sent, [(6, 0.0)])
        self.assertEqual(len(self.entries), 6)  # Shared cache is intact.

    async def test_existing_stream_continues_in_order_after_join(self):
        def advance():
            if len(self.sent) == 1:
                self.entries.extend([(42000, 6000), (48000, 6000)])
            if len(self.sent) == 3:
                self.sess.stopped = True

        await self.run_stream(advance)
        self.assertEqual(self.sent, [(6, 0.0), (7, 6.0), (8, 6.0)])
        self.assertEqual(self.sent_at, [self.sent_at[0]] * 3)  # No second startup wait.

    async def test_cold_channel_waits_then_joins_latest_ready(self):
        self.sess.ready_entries.side_effect = lambda **kw: list(self.entries) if self.clock else []
        await self.run_stream(lambda: setattr(self.sess, "stopped", True))
        self.assertEqual(self.sent, [(6, 0.0)])

    async def test_cold_channel_with_one_segment_still_starts(self):
        self.entries[:] = self.entries[:1]
        await self.run_stream(lambda: setattr(self.sess, "stopped", True))
        self.assertEqual(self.sent, [(1, 0.0)])

    async def test_initial_margin_is_three_seconds_for_six_second_segments(self):
        await self.run_stream(lambda: setattr(self.sess, "stopped", True))
        self.assertGreaterEqual(self.sent_at[0], 3.0)
        self.assertLess(self.sent_at[0], 3.13)

    async def test_short_segments_wait_only_half_a_segment(self):
        self.entries[:] = [(6000, 2000)]
        await self.run_stream(lambda: setattr(self.sess, "stopped", True))
        self.assertGreaterEqual(self.sent_at[0], 1.0)
        self.assertLess(self.sent_at[0], 1.13)

    async def test_long_segments_do_not_wait_more_than_three_seconds(self):
        self.entries[:] = [(6000, 20000)]
        await self.run_stream(lambda: setattr(self.sess, "stopped", True))
        self.assertGreaterEqual(self.sent_at[0], 3.0)
        self.assertLess(self.sent_at[0], 3.13)

    async def test_margin_starts_when_media_is_ready_not_when_request_arrives(self):
        self.sess.ready_entries.side_effect = lambda **kw: list(self.entries) if self.clock >= 10 else []
        await self.run_stream(lambda: setattr(self.sess, "stopped", True))
        self.assertGreaterEqual(self.sent_at[0], 13.0)
        self.assertLess(self.sent_at[0], 13.25)

    async def test_newer_segment_during_wait_does_not_erase_the_margin(self):
        def ready(**kwargs):
            return list(self.entries) + ([(42000, 6000)] if self.clock >= 1 else [])
        self.sess.ready_entries.side_effect = ready
        await self.run_stream(lambda: setattr(self.sess, "stopped", True))
        self.assertEqual(self.sent, [(6, 0.0)])
        self.assertGreaterEqual(self.sent_at[0], 3.0)

    async def test_evicted_start_segment_is_reselected_from_latest_available(self):
        self.sess.ready_entries.side_effect = lambda **kw: list(self.entries) if self.clock < 1 else [(42000, 6000)]
        await self.run_stream(lambda: setattr(self.sess, "stopped", True))
        self.assertEqual(self.sent, [(7, 0.0)])
        self.assertGreaterEqual(self.sent_at[0], 4.0)
        self.assertLess(self.sent_at[0], 4.3)

    async def test_wait_is_capped_by_the_existing_startup_deadline(self):
        self.sess.ready_entries.side_effect = lambda **kw: list(self.entries) if self.clock >= 24 else []
        await self.run_stream(lambda: setattr(self.sess, "stopped", True))
        self.assertGreaterEqual(self.sent_at[0], 25.0)
        self.assertLess(self.sent_at[0], 25.13)

    async def test_no_media_still_exits_after_existing_deadline(self):
        self.entries.clear()
        await self.run_stream(lambda: self.fail('no media available'))
        self.assertFalse(self.sent)
        self.assertGreater(self.clock, 25)
        self.assertLess(self.clock, 25.13)

    async def test_disconnect_during_margin_does_not_send_media(self):
        self.request.transport.is_closing = lambda: self.clock >= 1
        await self.run_stream(lambda: setattr(self.sess, "stopped", True))
        self.assertFalse(self.sent)
        self.assertLess(self.clock, 1.13)

    async def test_shutdown_during_margin_does_not_send_media(self):
        def stop():
            if self.clock >= 1:
                self.sess.stopped = True
        self.on_sleep = stop
        await self.run_stream(lambda: setattr(self.sess, "stopped", True))
        self.assertFalse(self.sent)
        self.assertLess(self.clock, 1.13)

    async def test_track_change_during_margin_does_not_send_stale_media(self):
        self.sess.generation = 0
        def change():
            if self.clock >= 1:
                self.sess.generation = 1
        self.on_sleep = change
        with self.assertLogs(proxy.log, level='WARNING'):
            await self.run_stream(lambda: setattr(self.sess, "stopped", True))
        self.assertFalse(self.sent)


class StartupCadenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_margin_covers_measured_segment_readiness_jitter(self):
        # Readiness times measured in the isolated CDN diagnostic. No network.
        ready_at = [1.74, 9.97, 15.45, 19.32, 23.31]
        clock = 0.0
        first = None
        delivered = []
        real_sleep = asyncio.sleep
        async def sleep(delay):
            nonlocal clock
            clock += delay
            await real_sleep(0)
        with tempfile.TemporaryDirectory(prefix='shaka-cadence-') as directory:
            sess = SimpleNamespace(
                ch=SimpleNamespace(slug='fixture'), hls_dir=Path(directory),
                video=SimpleNamespace(timescale=1000), stopped=False,
                viewers=0, clients={}, start_producer=Mock(), touch=Mock(),
                ready_entries=lambda **kw: [(6000*(n+1), 6000) for n,t in enumerate(ready_at) if t<=clock])
            for n in range(1,6):
                (sess.hls_dir/f'seg_{n*6000}.ts').write_bytes(bytes([n])*41360)
            request = SimpleNamespace(
                app={'state': SimpleNamespace(get_session=AsyncMock(return_value=sess))},
                match_info={'slug':'fixture'}, headers={}, remote='127.0.0.1',
                transport=SimpleNamespace(is_closing=lambda:False))
            response = SimpleNamespace(prepare=AsyncMock(), write_eof=AsyncMock())
            async def send(resp,data,duration=0.0,continuity=None):
                nonlocal first
                n=data[0]
                if first is None:
                    first=clock
                for part in range(60):
                    # Compare media time with wall time immediately before delivery.
                    delivered.append(((n-1)*6+part*.1,clock-first))
                    if duration:
                        await sleep(duration*.98/60)
                if n==5:
                    sess.stopped=True
            with patch.object(proxy.web,'StreamResponse',return_value=response), \
                    patch.object(proxy,'write_ts',side_effect=send), \
                    patch.object(proxy,'time',SimpleNamespace(monotonic=lambda:clock,time=lambda:0)), \
                    patch.object(proxy.asyncio,'sleep',side_effect=sleep):
                await asyncio.wait_for(proxy.handle_stream(request),2)
            self.assertTrue(delivered)
            self.assertGreaterEqual(min(media-wall for media,wall in delivered),-.05)
            self.assertEqual(sess.viewers,0)


if __name__ == "__main__":
    unittest.main()
