"""Regressions for long-lived sessions; no CDN or media processes required."""
import asyncio
import tempfile
import unittest
from types import SimpleNamespace

import proxy


class SegmentStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='shaka-state-')
        self.addCleanup(self.tmp.cleanup)
        self.sess = proxy.ChannelSession(
            SimpleNamespace(slug='fixture'), {'hls_dir': self.tmp.name}, None)
        self.sess.video = SimpleNamespace(entries=[], timescale=25)

    def advance_manifest(self, t):
        self.sess.video.entries = [(t + k * 150, 150) for k in range(-13, 3)]
        self.sess._next_t = t + 150

    async def test_day_of_segments_has_bounded_state(self):
        # Exercise the real lock, sequence allocator and pruning, not remux.
        for n in range(1, 14401):
            t = 1700000000 * 25 + n * 150
            self.advance_manifest(t)
            async with self.sess._segment_work(t):
                pass
            self.sess._published[t] = 150
            self.sess._seq(t)
            if n % 100 == 0:
                self.sess._skip_t.add(t - 1)
                self.sess._fail_t[t - 1] = 2
            self.sess._prune_cache()
        for name in ('locks', 't_to_seq', '_skip_t', '_fail_t'):
            with self.subTest(state=name):
                self.assertLessEqual(len(getattr(self.sess, name)), 16)
        self.assertEqual(len(self.sess._published), 6)
        self.assertEqual(self.sess.next_seq, 14401)

    async def test_failures_are_bounded_even_with_old_published_media(self):
        self.sess._published = {t: 150 for t in range(150, 1050, 150)}
        for n in range(100, 300):
            t = n * 150
            self.advance_manifest(t)
            async with self.sess._segment_work(t):
                pass
            self.sess._fail_t[t] = 2
            self.sess._skip_t.add(t)
            self.sess._prune_cache()
        for name in ('locks', '_skip_t', '_fail_t'):
            self.assertLessEqual(len(getattr(self.sess, name)), 16)
        self.assertEqual(len(self.sess._published), 6)

    def test_hls_sequence_and_transport_origin_survive_pruning(self):
        self.sess.ts_origin_t = 150
        for n in range(1, 31):
            t = n * 150
            self.advance_manifest(t)
            self.sess._published[t] = 150
            (self.sess.hls_dir / f'seg_{t}.ts').write_bytes(bytes(41000))
            self.sess._prune_cache()
            index = self.sess.build_index()
            self.assertIn(f'#EXT-X-MEDIA-SEQUENCE:{max(1, n - 5)}\n', index)
            self.assertEqual(self.sess.t_to_seq[t], n)
            self.assertEqual(self.sess.ts_origin_t, 150)
            self.assertEqual(self.sess._next_t, t + 150)
        self.assertEqual(self.sess.next_seq, 31)
        self.assertLessEqual(len(self.sess.t_to_seq), 16)

    def test_keep_recoverable_pending_and_published_state(self):
        self.advance_manifest(4500)
        # 2550 is still in the MPD but outside the six-file HLS window.
        # 150 is old published media; 2400 is a pending retry.
        self.sess._published = {150: 150}
        self.sess._next_t = 2400
        self.sess.t_to_seq = {0: 1, 150: 2, 2400: 3, 2550: 4, 4500: 5}
        self.sess.next_seq = 6
        self.sess._fail_t = {0: 2, 150: 2, 2400: 1, 2550: 2}
        self.sess._skip_t = {0, 150, 2550}
        self.sess._prune_cache()
        self.assertEqual(self.sess.t_to_seq, {150: 2, 2400: 3, 2550: 4, 4500: 5})
        self.assertEqual(self.sess._fail_t, {150: 2, 2400: 1, 2550: 2})
        self.assertEqual(self.sess._skip_t, {150, 2550})
        self.assertEqual(self.sess._seq(2550), 4)
        self.assertEqual(self.sess.next_seq, 6)

    def test_older_mpd_does_not_discard_future_sequence_history(self):
        self.advance_manifest(4500)
        self.sess.t_to_seq = {4500: 10, 4650: 11}
        self.sess.video.entries = [(1500, 150), (1650, 150)]
        self.sess._prune_cache()
        self.assertEqual(self.sess.t_to_seq, {4500: 10, 4650: 11})

    async def test_prune_protects_owner_waiter_and_awakened_waiter(self):
        self.advance_manifest(4500)
        entered, release = asyncio.Event(), asyncio.Event()
        observed = []
        async def waiter():
            async with self.sess._segment_work(150):
                observed.append(self.sess.locks[150])
                entered.set()
                await release.wait()
        task = None
        try:
            async with self.sess._segment_work(150):
                lock = self.sess.locks[150]
                self.sess.t_to_seq[150] = 7
                task = asyncio.create_task(waiter())
                await asyncio.sleep(0)
                self.sess._prune_cache()
                self.assertIs(self.sess.locks[150], lock)
                self.assertEqual(self.sess.t_to_seq[150], 7)
            # The lock is unlocked, but the awakened waiter has not resumed.
            self.assertFalse(lock.locked())
            self.sess._prune_cache()
            self.assertIs(self.sess.locks[150], lock)
            await asyncio.wait_for(entered.wait(), 2)
            self.assertEqual(observed, [lock])
            release.set()
            await asyncio.wait_for(task, 2)
            self.sess._prune_cache()
            self.assertNotIn(150, self.sess.locks)
            self.assertNotIn(150, self.sess.t_to_seq)
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_cancelled_waiter_does_not_allow_pruning_owner_lock(self):
        self.advance_manifest(4500)
        async def waiter():
            async with self.sess._segment_work(150):
                self.fail('cancelled waiter acquired the lock')
        async with self.sess._segment_work(150):
            lock = self.sess.locks[150]
            waiting = asyncio.create_task(waiter())
            await asyncio.sleep(0)
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            self.sess._prune_cache()
            self.assertIs(self.sess.locks[150], lock)
        self.sess._prune_cache()
        self.assertNotIn(150, self.sess.locks)

    async def test_cancelled_owner_and_exception_release_lock_usage(self):
        self.advance_manifest(4500)
        entered = asyncio.Event()
        async def owner():
            async with self.sess._segment_work(150):
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(owner())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with self.assertRaises(ValueError):
            async with self.sess._segment_work(300):
                raise ValueError('fixture')
        self.sess._prune_cache()
        self.assertFalse(self.sess.locks)

    async def test_many_contenders_remain_serialized_during_pruning(self):
        self.advance_manifest(4500)
        active = {}
        async def work(t):
            for _ in range(10):
                async with self.sess._segment_work(t):
                    active[t] = active.get(t, 0) + 1
                    self.assertEqual(active[t], 1)
                    self.sess._prune_cache()
                    await asyncio.sleep(0)
                    self.assertEqual(active[t], 1)
                    active[t] -= 1
                self.sess._prune_cache()
        await asyncio.wait_for(asyncio.gather(
            *[work(t) for t in (150, 300) for _ in range(20)]), 5)
        self.sess._prune_cache()
        self.assertFalse(self.sess.locks)
        self.assertFalse(self.sess._lock_users)


if __name__ == '__main__':
    unittest.main()
