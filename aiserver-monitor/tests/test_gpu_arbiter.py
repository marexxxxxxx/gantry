import asyncio
import time
import unittest

from aiserver_monitor.autoload import ProviderUnavailable
from aiserver_monitor.config import Config
from aiserver_monitor.gpu_arbiter import (
    ArbiterState, ClientDisconnected, GpuArbiter, JobResult)
from aiserver_monitor.jobs import JobRegistry, JobStatus


class _FakeServerControl:
    """Duck-Type fuer ServerControl: merkt stop/start-Aufrufe und simuliert
    den Backend-Zustand ueber ``snapshot()["backend"]``."""

    def __init__(self, backend="edge0", fail_start=False, fail_stop=False):
        self.backend = backend
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.stopped: list[str] = []
        self.started: list[str] = []

    async def snapshot(self):
        return {"backend": self.backend}

    async def stop_and_wait(self, backend, *, timeout_s=None):
        if self.fail_stop:
            raise RuntimeError("stop fehlgeschlagen")
        self.stopped.append(backend)
        self.backend = None

    async def start_and_wait(self, backend, *, timeout_s=None):
        if self.fail_start:
            raise RuntimeError("start fehlgeschlagen")
        self.started.append(backend)
        self.backend = backend


def _make_arbiter(config=None, server=None, inflight=None,
                  sleeper=asyncio.sleep, clock=time.monotonic,
                  subprocess_factory=asyncio.create_subprocess_exec):
    cfg = config or Config()
    return GpuArbiter(
        cfg,
        server if server is not None else _FakeServerControl(),
        inflight=inflight if inflight is not None else (lambda: 0),
        sleeper=sleeper,
        clock=clock,
        subprocess_factory=subprocess_factory,
    )


class _FakeProcess:
    """Fake fuer den Subprozess: liefert festes stdout/stderr und returncode."""

    def __init__(self, returncode=0):
        self.returncode = returncode

    async def communicate(self):
        return (b"out", b"err")


def _subprocess_factory_with(returncode=0):
    async def factory(*args, **kwargs):
        return _FakeProcess(returncode)
    return factory


class _HangingProcess:
    """Fake-Prozess, der haengt, bis ``kill()`` ihn beendet."""

    def __init__(self):
        self.returncode = None
        self.killed = False
        self._done = asyncio.Event()

    async def communicate(self):
        await self._done.wait()
        self.returncode = -9
        return (b"", b"")

    def kill(self):
        self.killed = True
        self._done.set()


class _DisconnectedRequest:
    async def is_disconnected(self):
        return True


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

    async def test_run_job_liefert_exit_code_und_output(self):
        server = _FakeServerControl("edge0")
        arb = _make_arbiter(
            server=server,
            subprocess_factory=_subprocess_factory_with(0),
        )
        result = await arb.run_job(["echo", "hi"])
        self.assertIsInstance(result, JobResult)
        self.assertTrue(result.lease_id)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "out")
        self.assertEqual(result.stderr, "err")
        self.assertEqual(arb.state, ArbiterState.IDLE)
        self.assertEqual(server.started, ["edge0"])

    async def test_run_job_gibt_lease_bei_fehlschlag_frei(self):
        server = _FakeServerControl("edge0")
        arb = _make_arbiter(
            server=server,
            subprocess_factory=_subprocess_factory_with(2),
        )
        result = await arb.run_job(["false"])
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(arb.state, ArbiterState.IDLE)
        self.assertEqual(server.started, ["edge0"])

    async def test_complete_ueber_webhook_resumet(self):
        server = _FakeServerControl("edge0")
        arb = _make_arbiter(server=server)
        lease = await arb.request_lease(completion="webhook")
        self.assertEqual(arb.state, ArbiterState.PAUSED)
        await arb.complete(lease.lease_id, exit_code=0)
        self.assertEqual(arb.state, ArbiterState.IDLE)
        self.assertEqual(server.started, ["edge0"])

    async def test_watchdog_forciert_completion_nach_timeout(self):
        clock = [0.0]
        server = _FakeServerControl("edge0")
        arb = _make_arbiter(server=server, clock=lambda: clock[0])
        lease = await arb.request_lease(timeout_s=1.0)
        self.assertEqual(arb.state, ArbiterState.PAUSED)
        self.assertEqual(server.stopped, ["edge0"])
        clock[0] = 2.0  # Deadline (0.0 + 1.0) ueberschritten
        await arb.watchdog_once()
        self.assertEqual(arb.state, ArbiterState.IDLE)
        self.assertEqual(server.started, ["edge0"])

    async def test_run_job_killt_subprozess_bei_timeout(self):
        proc = _HangingProcess()

        async def factory(*args, **kwargs):
            return proc

        server = _FakeServerControl("edge0")
        arb = _make_arbiter(server=server, subprocess_factory=factory)
        result = await arb.run_job(["blender", "-b", "x"], timeout_s=0.1)
        self.assertTrue(proc.killed, "Subprozess muss bei Timeout gekillt werden")
        self.assertIsNone(result.exit_code)
        self.assertEqual(arb.state, ArbiterState.IDLE)

    async def test_watchdog_killt_aktiven_subprozess(self):
        clock = [0.0]
        proc = _HangingProcess()
        server = _FakeServerControl("edge0")
        arb = _make_arbiter(server=server, clock=lambda: clock[0])
        await arb.request_lease(timeout_s=1.0)
        arb._active_proc = proc  # simuliert einen laufenden run_job
        clock[0] = 2.0
        await arb.watchdog_once()
        self.assertTrue(proc.killed, "Watchdog muss den Subprozess killen")
        self.assertEqual(arb.state, ArbiterState.IDLE)

    async def test_wait_serving_raises_bei_error(self):
        server = _FakeServerControl("edge0", fail_start=True)
        arb = _make_arbiter(server=server)
        lease = await arb.request_lease()
        await arb.release(lease.lease_id)  # Resume schlaegt fehl -> ERROR
        self.assertEqual(arb.state, ArbiterState.ERROR)
        with self.assertRaises(ProviderUnavailable):
            await arb.wait_serving()

    async def test_wait_serving_bricht_bei_disconnect_ab(self):
        arb = _make_arbiter()
        await arb.request_lease()  # PAUSED
        with self.assertRaises(ClientDisconnected):
            await arb.wait_serving(_DisconnectedRequest())

    async def test_drain_wartet_auf_gateway_active(self):
        server = _FakeServerControl("edge0")
        arb = _make_arbiter(server=server)
        await arb.wait_serving()  # reserviert einen Gateway-Slot
        task = asyncio.create_task(arb.request_lease())
        await asyncio.sleep(0.01)
        self.assertEqual(arb.state, ArbiterState.PAUSING)
        self.assertFalse(task.done(), "Drain muss auf den Gateway-Slot warten")
        arb.leave_serving()
        await asyncio.wait_for(task, timeout=5.0)
        self.assertEqual(arb.state, ArbiterState.PAUSED)

    async def test_drain_fehler_setzt_error(self):
        server = _FakeServerControl("edge0", fail_stop=True)
        arb = _make_arbiter(server=server)
        with self.assertRaises(RuntimeError):
            await arb.request_lease()
        self.assertEqual(arb.state, ArbiterState.ERROR)


if __name__ == "__main__":
    unittest.main()
