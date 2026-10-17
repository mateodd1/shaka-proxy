"""Watched channels get origin download slots before warm, unwatched ones."""
import asyncio
import tempfile
import time
import unittest
from types import SimpleNamespace

import proxy


class PrioritySemaphoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_urgent_waiters_are_served_first(self):
        sem = proxy.PrioritySemaphore(1)
        order = []
        await sem.acquire()

        async def worker(name, urgent):
            async with sem.slot(urgent):
                order.append(name)

        tasks = [asyncio.create_task(worker("idle-1", False)),
                 asyncio.create_task(worker("idle-2", False))]
        await asyncio.sleep(0)
        tasks.append(asyncio.create_task(worker("watched", True)))
        await asyncio.sleep(0)
        sem.release()
        await asyncio.gather(*tasks)
        self.assertEqual(order, ["watched", "idle-1", "idle-2"])

    async def test_limits_concurrency_like_a_semaphore(self):
        sem = proxy.PrioritySemaphore(2)
        running = peak = 0

        async def worker():
            nonlocal running, peak
            async with sem:
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.01)
                running -= 1

        await asyncio.gather(*[worker() for _ in range(6)])
        self.assertEqual(peak, 2)
        self.assertEqual(sem._value, 2)

    async def test_cancelled_waiter_does_not_leak_a_slot(self):
        sem = proxy.PrioritySemaphore(1)
        await sem.acquire()
        waiter = asyncio.create_task(sem.acquire(True))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        sem.release()
        self.assertEqual(sem._value, 1)
        await asyncio.wait_for(sem.acquire(), 1)

    async def test_granted_then_cancelled_waiter_returns_the_slot(self):
        sem = proxy.PrioritySemaphore(1)
        await sem.acquire()
        waiter = asyncio.create_task(sem.acquire())
        await asyncio.sleep(0)
        sem.release()  # hands the slot to the waiter...
        waiter.cancel()  # ...which is cancelled before it resumes
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(sem._value, 1)


class WatchedTests(unittest.TestCase):
    def test_viewers_or_recent_clients_mark_a_channel_watched(self):
        with tempfile.TemporaryDirectory() as hls:
            sess = proxy.ChannelSession(SimpleNamespace(slug="c", keys={}), {"hls_dir": hls},
                                        SimpleNamespace())
            self.assertTrue(sess.watched())  # just opened
            sess.last_access = time.monotonic() - 60
            self.assertFalse(sess.watched())  # left after a zap
            sess.viewers = 1
            self.assertTrue(sess.watched())


if __name__ == "__main__":
    unittest.main()
