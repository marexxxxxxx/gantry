import asyncio
import unittest

from aiserver_monitor.config import Config
from aiserver_monitor.gpu_arbiter import ArbiterState, GpuArbiter
from aiserver_monitor.jobs import JobRegistry, JobStatus


class _FakeServerControl:
    """Duck-Type fuer ServerControl: merkt stop/start-Aufrufe und simuliert
    den Backend-Zustand ueber ``snapshot()["backend"]``."""

    def __init__(self, backend="edge0", fail_start=False):
        self.backend = backend
        self.fail_start = fail_start
        self.stopped: list[str] = []
        self.started: list[str] = []

    def snapshot(self):
        return {"backend": self.backend}

    async def stop_and_wait(self, backend, *, timeout_s=None):
        self.stopped.append(backend)
        self.backend = None

    async def start_and_wait(self, backend, *, timeout_s=None):
        if self.fail_start:
            raise RuntimeError("start fehlgeschlagen")
        self.started.append(backend)
        self.backend = backend


def _make_arbiter(config=None, server=None, inflight=None,
                  sleeper=asyncio.sleep):
    cfg = config or Config()
    return GpuArbiter(
        cfg,
        server if server is not None else _FakeServerControl(),
        inflight=inflight if inflight is not None else (lambda: 0),
        sleeper=sleeper,
    )


class TestJobRegistryActiveCount(unittest.TestCase):
    def test_active_count_zaehlt_waiting_und_running(self):
        reg = JobRegistry(4096)
        self.assertEqual(reg.active_count(), 0)
        reg.register("a", source="x", model="m", full_prompt="p", now=0.0)
        self.assertEqual(reg.active_count(), 1)
        reg.note_running("a", now=1.0)
        self.assertEqual(reg.active_count(), 1)
        reg.finish("a", JobStatus.DONE, now=2.0)
        self.assertEqual(reg.active_count(), 0)


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
        await asyncio.sleep(0)
        self.assertFalse(task.done(), "wait_serving muss waehrend PAUSED halten")
        await arb.release(lease.lease_id)
        await asyncio.wait_for(task, timeout=1.0)
        self.assertEqual(arb.state, ArbiterState.IDLE)

    async def test_pausing_wartet_bis_inflight_null(self):
        inflight = [1]
        server = _FakeServerControl("edge0")
        arb = _make_arbiter(server=server, inflight=lambda: inflight[0])
        task = asyncio.create_task(arb.request_lease())
        await asyncio.sleep(0.01)
        self.assertEqual(arb.state, ArbiterState.PAUSING)
        self.assertFalse(task.done(), "request_lease darf bei inflight>0 nicht fertig sein")
        inflight[0] = 0
        await asyncio.wait_for(task, timeout=5.0)
        self.assertEqual(arb.state, ArbiterState.PAUSED)
        self.assertEqual(server.stopped, ["edge0"])

    async def test_resume_laedt_previous_backend(self):
        server = _FakeServerControl("edge0")
        arb = _make_arbiter(server=server)
        lease = await arb.request_lease()
        self.assertEqual(server.stopped, ["edge0"])
        await arb.release(lease.lease_id)
        self.assertEqual(server.started, ["edge0"])
        self.assertEqual(arb.state, ArbiterState.IDLE)

    async def test_kette_laedt_nicht_neu_wenn_queue_nicht_leer(self):
        server = _FakeServerControl("edge0")
        arb = _make_arbiter(server=server)
        lease1 = await arb.request_lease()
        lease2 = await arb.request_lease()
        self.assertEqual(lease2.state, "queued")
        await arb.complete(lease1.lease_id)
        # Ketten-Lease: zweiter Lease uebernimmt ohne Reload.
        self.assertEqual(arb.state, ArbiterState.PAUSED)
        self.assertEqual(server.started, [])
        await arb.complete(lease2.lease_id)
        self.assertEqual(server.started, ["edge0"])
        self.assertEqual(arb.state, ArbiterState.IDLE)

    async def test_resume_fehler_setzt_error(self):
        server = _FakeServerControl("edge0", fail_start=True)
        arb = _make_arbiter(server=server)
        lease = await arb.request_lease()
        await arb.release(lease.lease_id)
        self.assertEqual(arb.state, ArbiterState.ERROR)


if __name__ == "__main__":
    unittest.main()
