import asyncio
import unittest

from aiserver_monitor.config import Config
from aiserver_monitor.gpu_arbiter import ArbiterState, GpuArbiter


class _FakeServerControl:
    """Minimaler Duck-Type: liefert nur snapshot() wie ServerControl."""

    def snapshot(self):
        return {"backend": None}


def _make_arbiter(config=None, inflight=None):
    cfg = config or Config()
    return GpuArbiter(
        cfg,
        _FakeServerControl(),
        inflight=inflight if inflight is not None else (lambda: 0),
    )


class TestGpuArbiter(unittest.IsolatedAsyncioTestCase):
    async def test_request_lease_in_idle_granted(self):
        arb = _make_arbiter()
        lease = await arb.request_lease()
        self.assertEqual(lease.state, "granted")
        self.assertEqual(lease.position, 0)
        self.assertTrue(lease.lease_id)
        self.assertEqual(arb.state, ArbiterState.PAUSED)

    async def test_zweiter_lease_waehrend_paused_queued(self):
        arb = _make_arbiter()
        first = await arb.request_lease()
        second = await arb.request_lease()
        self.assertEqual(first.state, "granted")
        self.assertEqual(first.position, 0)
        self.assertEqual(second.state, "queued")
        self.assertEqual(second.position, 1)

    async def test_wait_serving_blockiert_bis_idle(self):
        arb = _make_arbiter()
        lease = await arb.request_lease()
        task = asyncio.create_task(arb.wait_serving())
        await asyncio.sleep(0)  # Task anlaufen lassen
        self.assertFalse(task.done(), "wait_serving muss waehrend PAUSED halten")
        await arb.release(lease.lease_id)
        await asyncio.wait_for(task, timeout=1.0)
        self.assertEqual(arb.state, ArbiterState.IDLE)


if __name__ == "__main__":
    unittest.main()
