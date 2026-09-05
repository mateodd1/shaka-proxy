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
        for n in range(1, 9):
            # Distinct payloads identify the selected segment, above the size
            # threshold. Codec handling is unrelated to the join cursor.
            (self.sess.hls_dir / f"seg_{n * 6000}.ts").write_bytes(bytes([n]) * 41360)

    async def run_stream(self, on_write):
        async def send(resp, data, duration=0.0, continuity=None):
            self.assertIs(resp, self.resp)
            self.sent.append((data[0], duration))
            on_write()

        with patch.object(proxy.web, "StreamResponse", return_value=self.resp), \
                patch.object(proxy, "write_ts", side_effect=send):
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

    async def test_cold_channel_waits_then_joins_latest_ready(self):
        self.sess.ready_entries.side_effect = [[], list(self.entries)]
        await self.run_stream(lambda: setattr(self.sess, "stopped", True))
        self.assertEqual(self.sent, [(6, 0.0)])

    async def test_cold_channel_with_one_segment_still_starts(self):
        self.entries[:] = self.entries[:1]
        await self.run_stream(lambda: setattr(self.sess, "stopped", True))
        self.assertEqual(self.sent, [(1, 0.0)])


if __name__ == "__main__":
    unittest.main()
