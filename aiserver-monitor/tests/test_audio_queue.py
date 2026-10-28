import asyncio
import unittest

from aiserver_audio.queue import Admission, QueueFull


class TestAdmission(unittest.IsolatedAsyncioTestCase):
    async def test_dritter_wartender_wird_abgewiesen(self):
        admission = Admission(parallel=1, max_queue=1)
        await admission.acquire()
        wartend = asyncio.create_task(admission.acquire())
        await asyncio.sleep(0)
        self.assertEqual(admission.waiting, 1)
        with self.assertRaises(QueueFull) as ctx:
            await admission.acquire()
        self.assertEqual(ctx.exception.retry_after, 5)

        admission.release()
        await asyncio.wait_for(wartend, timeout=1)
        self.assertEqual(admission.waiting, 0)
        admission.release()

    async def test_parallel_zwei(self):
        admission = Admission(parallel=2, max_queue=1)
        await admission.acquire()
        await admission.acquire()
        wartend = asyncio.create_task(admission.acquire())
        await asyncio.sleep(0)
        self.assertEqual(admission.waiting, 1)
        with self.assertRaises(QueueFull):
            await admission.acquire()
        admission.release()
        await asyncio.wait_for(wartend, timeout=1)
        admission.release()
        admission.release()

    async def test_abbruch_senkt_wartende(self):
        admission = Admission(parallel=1, max_queue=1)
        await admission.acquire()
        wartend = asyncio.create_task(admission.acquire())
        await asyncio.sleep(0)
        wartend.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await wartend
        self.assertEqual(admission.waiting, 0)
        admission.release()
