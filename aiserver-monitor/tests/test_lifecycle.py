"""Tests fuer den Modell-Lebenszyklus (/v1/load, /v1/unload, /v1/status).

Die Tests laufen ohne echte Sockets: ServerControl, Ollama und der
Sidecar-Spawner sind Fakes. Damit pruefen wir genau die Vertraege, die der
Aufrufer sieht (Profilnamen, Reihenfolge, Fehler) und die Aufrufe, die am
Backend ankommen.
"""

import json
import unittest
from pathlib import Path

import httpx

from aiserver_monitor.config import Config
from aiserver_monitor.lifecycle import (
    LifecycleConflictError,
    LifecycleDisabledError,
    LifecycleUnavailableError,
    ModelLifecycle,
    ProfileCatalog,
    UnknownProfileError,
)

EMBED = "qwen3-embedding:0.6b"
QWEN = "escha-qwen38-27b-w2"
FLINT = "hf.co/AtomicChat/Flint-GGUF:IQ4_XS"
TTS = "Qwen3-TTS"
AUDIO8 = "Audio8-Realistic-Air"


class FakeClock:
    """Monotone Uhr, die nur beim Schlafen fortschreitet."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, sekunden: float) -> None:
        self.slept.append(sekunden)
        self.now += sekunden


class FakeServerControl:
    """ServerControl-Fake mit aufzeichnenden start/stop-Aufrufen."""

    def __init__(self, *, state="stopped", backend=None, enabled=True,
                 start_delay=0, on_running=None):
        self.state = state
        self.backend = backend
        self.started = []
        self.stopped = []
        self.error = None
        self.conflicts = 0
        self.start_delay = start_delay
        self.on_running = on_running
        self._countdown = None

    async def snapshot(self):
        if self._countdown is not None:
            self._countdown -= 1
            if self._countdown <= 0:
                self.state = "running"
                self._countdown = None
                if self.on_running is not None:
                    self.on_running()
        return {
            "state": self.state,
            "backend": self.backend,
            "backends": [],
            "unit": "escha-server.service",
            "managed_by_systemd": False,
            "upstream": "http://127.0.0.1:30001",
            "error": self.error,
        }

    async def start(self, backend=None):
        if self.conflicts > 0:
            self.conflicts -= 1
            from aiserver_monitor.serverctl import ServerConflictError
            raise ServerConflictError("Uebergang laeuft")
        self.started.append(backend)
        self.backend = backend or "qwen"
        if self.start_delay > 0:
            self.state = "starting"
            self._countdown = self.start_delay
        else:
            self.state = "running"
            if self.on_running is not None:
                self.on_running()
        return await self.snapshot()

    async def stop(self, backend=None):
        self.stopped.append(backend)
        self.state = "stopped"
        self.backend = None
        return await self.snapshot()


class FakeOllama:
    """Ollama-Fake fuer /api/version, /api/ps, /api/generate und /api/embed."""

    def __init__(self, *, version=True):
        self.version = version
        self.models = set()
        self.calls = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        pfad = request.url.path
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            body = {}
        self.calls.append((pfad, body))
        if pfad == "/api/version":
            if not self.version:
                return httpx.Response(503, json={"error": "nicht bereit"})
            return httpx.Response(200, json={"version": "0.0.0"})
        if pfad == "/api/ps":
            return httpx.Response(200, json={
                "models": [{"name": name} for name in sorted(self.models)],
            })
        if pfad in ("/api/generate", "/api/embed"):
            if not self.version:
                return httpx.Response(503, json={"error": "nicht bereit"})
            model = body.get("model")
            if body.get("keep_alive") == 0:
                self.models.discard(model)
            elif model:
                self.models.add(model)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    def paths(self) -> list[str]:
        return [pfad for pfad, _ in self.calls]

    def body_for(self, pfad: str):
        return [body for p, body in self.calls if p == pfad]


class FakeTTS:
    """TTS-Worker-Fake fuer /healthz, /load und /unload."""

    def __init__(self):
        self.alive = False
        self.model_loaded = False
        self.calls = []
        self.load_status = 200

    def handle(self, request: httpx.Request) -> httpx.Response:
        if not self.alive:
            raise httpx.ConnectError("down", request=request)
        pfad = request.url.path
        self.calls.append(pfad)
        if pfad == "/healthz":
            return httpx.Response(200, json={
                "status": "ok", "model_loaded": self.model_loaded})
        if pfad == "/load":
            if self.load_status != 200:
                return httpx.Response(self.load_status, json={"error": "oom"})
            self.model_loaded = True
            return httpx.Response(200, json={"status": "ok",
                                             "model_loaded": True})
        if pfad == "/unload":
            self.model_loaded = False
            return httpx.Response(200, json={"status": "ok",
                                             "model_loaded": False})
        return httpx.Response(404)


class FakeSpawner:
    """Ersetzt subprocess.Popen; zeichnet (args, env) auf."""

    def __init__(self, on_spawn=None):
        self.calls = []
        self.on_spawn = on_spawn

    def __call__(self, args, env):
        self.calls.append((args, env))
        if self.on_spawn is not None:
            self.on_spawn()
        return object()


class LifecycleTestCase(unittest.IsolatedAsyncioTestCase):
    """Gemeinsame Fakes: Chat-Ollama auf :30001, Embed-Ollama auf :30002."""

    async def asyncSetUp(self):
        self.chat = FakeOllama()
        self.embed = FakeOllama()
        self.qwen_tts = FakeTTS()
        self.audio8 = FakeTTS()
        self.transport = httpx.MockTransport(self._handler)
        self.client = httpx.AsyncClient(transport=self.transport)
        self.addAsyncCleanup(self.client.aclose)
        self.control = FakeServerControl()
        self.clock = FakeClock()
        self.config = Config(db_path="/tmp/aiserver-monitor-lifecycle.db")

    def _handler(self, request):
        port = request.url.port
        if port == 30001:
            return self.chat.handle(request)
        if port == 30002:
            return self.embed.handle(request)
        if port == 30005:
            return self.qwen_tts.handle(request)
        if port == 30006:
            return self.audio8.handle(request)
        return httpx.Response(404)

    def _qwen_start(self):
        self.qwen_tts.alive = True

    def _qwen_stop(self):
        self.qwen_tts.alive = False

    def _audio8_start(self):
        self.audio8.alive = True

    def _audio8_stop(self):
        self.audio8.alive = False

    def lifecycle(self, *, config=None, control=None, spawner=None):
        return ModelLifecycle(
            config or self.config,
            control or self.control,
            client=self.client,
            repo_root=Path("/repo"),
            spawner=spawner or FakeSpawner(),
            clock=self.clock,
            sleeper=self.clock.sleep,
            poll_s=1.0,
            load_timeout_s=10.0,
            unload_timeout_s=10.0,
        )


class TestProfileCatalog(unittest.TestCase):
    def test_namen_bleiben_exakt_und_in_konfigurationsreihenfolge(self):
        katalog = ProfileCatalog(Config(db_path="x.db"))
        self.assertEqual(katalog.names, [QWEN, FLINT, "gpt-oss:20b",
                                         "hf.co/bartowski/huihui-ai_Qwen3-14B-"
                                         "abliterated-GGUF:Q4_K_M", TTS, AUDIO8,
                                         EMBED])

    def test_doppelte_namen_werden_zuerst_gewonnen(self):
        katalog = ProfileCatalog(Config(
            db_path="x.db", flint_model=QWEN, embed_model=EMBED,
        ))
        self.assertEqual(katalog.names, [QWEN, "gpt-oss:20b",
                                         "hf.co/bartowski/huihui-ai_Qwen3-14B-"
                                         "abliterated-GGUF:Q4_K_M", TTS, AUDIO8,
                                         EMBED])
        self.assertEqual(katalog.get(QWEN).backend, "qwen")

    def test_leere_namen_werden_uebersprungen(self):
        katalog = ProfileCatalog(Config(
            db_path="x.db", model_alias="", flint_model="", gptoss_model="",
            abl_model="", qwen_tts_model="", audio8_model="", embed_model="",
        ))
        self.assertEqual(katalog.names, [])

    def test_katalog_enthaelt_qwen3_tts(self):
        katalog = ProfileCatalog(Config(db_path="x.db"))
        self.assertIn(TTS, katalog.names)
        self.assertEqual(katalog.get(TTS).backend, "qwen-tts")
        self.assertEqual(katalog.get(TTS).kind, "tts")

    def test_katalog_enthaelt_audio8(self):
        katalog = ProfileCatalog(Config(db_path="x.db"))
        self.assertIn(AUDIO8, katalog.names)
        self.assertEqual(katalog.get(AUDIO8).backend, "audio8")
        self.assertEqual(katalog.get(AUDIO8).kind, "tts")

    def test_resolve_prueft_alles_vor_der_ersten_aktion(self):
        katalog = ProfileCatalog(Config(db_path="x.db"))
        with self.assertRaises(UnknownProfileError) as ctx:
            katalog.resolve([QWEN, "gibt-es-nicht", EMBED])
        self.assertEqual(ctx.exception.unknown, ["gibt-es-nicht"])
        self.assertIn(QWEN, ctx.exception.profiles)

    def test_resolve_entfernt_duplikate_und_behaelt_reihenfolge(self):
        katalog = ProfileCatalog(Config(db_path="x.db"))
        self.assertEqual([p.name for p in katalog.resolve([EMBED, QWEN, EMBED])],
                         [EMBED, QWEN])

    def test_namen_werden_nicht_getrimmt(self):
        katalog = ProfileCatalog(Config(db_path="x.db"))
        with self.assertRaises(UnknownProfileError):
            katalog.resolve([" " + QWEN])


class TestStatus(LifecycleTestCase):
    async def test_leer(self):
        status = await self.lifecycle().status()
        self.assertEqual(status["profiles"], ProfileCatalog(self.config).names)
        self.assertEqual(status["loaded"], [])

    async def test_qwen_laeuft(self):
        self.control.state = "running"
        self.control.backend = "qwen"
        status = await self.lifecycle().status()
        self.assertEqual(status["loaded"], [
            {"profile": QWEN, "backend": "qwen", "kind": "chat"},
        ])

    async def test_flint_geladen(self):
        self.control.state = "running"
        self.control.backend = "flint"
        self.chat.models.add(FLINT)
        status = await self.lifecycle().status()
        self.assertEqual(status["loaded"], [
            {"profile": FLINT, "backend": "flint", "kind": "chat"},
        ])

    async def test_embedding_geladen(self):
        self.embed.models.add(EMBED)
        status = await self.lifecycle().status()
        self.assertEqual(status["loaded"], [
            {"profile": EMBED, "backend": "embed", "kind": "embedding"},
        ])

    async def test_backend_laeuft_aber_modell_nicht(self):
        self.control.state = "running"
        self.control.backend = "flint"
        status = await self.lifecycle().status()
        self.assertEqual(status["loaded"], [])

    async def test_status_meldet_qwen3_tts_nur_wenn_geladen(self):
        lifecycle = self.lifecycle()
        self.assertEqual((await lifecycle.status())["loaded"], [])
        self.qwen_tts.alive = True
        self.qwen_tts.model_loaded = False
        self.assertEqual((await lifecycle.status())["loaded"], [])
        self.qwen_tts.model_loaded = True
        geladen = (await lifecycle.status())["loaded"]
        self.assertEqual([e["profile"] for e in geladen], [TTS])

    async def test_status_trennt_qwen3_tts_und_audio8(self):
        lifecycle = self.lifecycle()
        self.qwen_tts.alive = True
        self.qwen_tts.model_loaded = True
        geladen = (await lifecycle.status())["loaded"]
        self.assertEqual([e["profile"] for e in geladen], [TTS])
        self.qwen_tts.model_loaded = False
        self.audio8.alive = True
        self.audio8.model_loaded = True
        geladen = (await lifecycle.status())["loaded"]
        self.assertEqual([e["profile"] for e in geladen], [AUDIO8])


class TestLoad(LifecycleTestCase):
    async def test_unbekannter_name_laedt_nichts(self):
        with self.assertRaises(UnknownProfileError):
            await self.lifecycle().load(["gibt-es-nicht"])
        self.assertEqual(self.control.started, [])

    async def test_zwei_chat_backends_sind_konflikt(self):
        with self.assertRaises(LifecycleConflictError):
            await self.lifecycle().load([QWEN, FLINT])
        self.assertEqual(self.control.started, [])

    async def test_deaktivierte_steuerung_verweigert_chat(self):
        self.config = Config(db_path="x.db", server_control_enabled=False)
        with self.assertRaises(LifecycleDisabledError):
            await self.lifecycle().load([QWEN])
        self.assertEqual(self.control.started, [])

    async def test_qwen_wird_gestartet(self):
        antwort = await self.lifecycle().load([QWEN])
        self.assertEqual(self.control.started, ["qwen"])
        self.assertEqual(antwort["requested"], [QWEN])
        self.assertEqual([p["profile"] for p in antwort["loaded"]], [QWEN])

    async def test_qwen_bereits_geladen_ist_no_op(self):
        self.control.state = "running"
        self.control.backend = "qwen"
        antwort = await self.lifecycle().load([QWEN])
        self.assertEqual(self.control.started, [])
        self.assertEqual([p["profile"] for p in antwort["loaded"]], [QWEN])

    async def test_flint_wird_vorgeladen(self):
        antwort = await self.lifecycle().load([FLINT])
        self.assertEqual(self.control.started, ["flint"])
        self.assertIn("/api/generate", self.chat.paths())
        self.assertEqual(self.chat.body_for("/api/generate")[-1]["model"], FLINT)
        self.assertEqual(self.chat.body_for("/api/generate")[-1]["keep_alive"],
                         -1)
        self.assertEqual([p["profile"] for p in antwort["loaded"]], [FLINT])

    async def test_wartet_auf_backendstart_vor_dem_vorladen(self):
        self.chat.version = False
        control = FakeServerControl(
            start_delay=2,
            on_running=lambda: setattr(self.chat, "version", True))
        antwort = await self.lifecycle(control=control).load([FLINT])
        self.assertEqual(control.started, ["flint"])
        self.assertEqual(self.chat.body_for("/api/generate")[-1]["model"], FLINT)
        self.assertEqual([p["profile"] for p in antwort["loaded"]], [FLINT])

    async def test_embedding_startet_sidecar_und_laedt(self):
        self.embed.version = False
        spawner = FakeSpawner(on_spawn=self._embed_bereit)
        antwort = await self.lifecycle(spawner=spawner).load([EMBED])

        self.assertEqual(len(spawner.calls), 1)
        args, env = spawner.calls[0]
        self.assertEqual(args, ["bash", "/repo/runtime/embed/start-embed.sh"])
        self.assertEqual(env["EMBED_HOST"], "127.0.0.1:30002")
        self.assertEqual(env["EMBED_MODEL"], EMBED)
        self.assertIn("/api/embed", self.embed.paths())
        self.assertEqual(self.embed.body_for("/api/embed")[-1]["keep_alive"], -1)
        self.assertEqual([p["profile"] for p in antwort["loaded"]], [EMBED])

    async def test_embedding_bereits_geladen_ist_no_op(self):
        self.embed.models.add(EMBED)
        spawner = FakeSpawner()
        antwort = await self.lifecycle(spawner=spawner).load([EMBED])
        self.assertEqual(spawner.calls, [])
        self.assertEqual(self.embed.body_for("/api/embed"), [])
        self.assertEqual([p["profile"] for p in antwort["loaded"]], [EMBED])

    async def test_chat_wechsel_meldet_entladenes_altes_modell(self):
        self.control.state = "running"
        self.control.backend = "qwen"
        antwort = await self.lifecycle().load([FLINT])
        self.assertEqual(self.control.started, ["flint"])
        self.assertEqual([p["profile"] for p in antwort["unloaded"]], [QWEN])

    async def test_load_qwen3_tts_startet_worker_und_laedt(self):
        spawner = FakeSpawner(on_spawn=self._qwen_start)
        lifecycle = self.lifecycle(spawner=spawner)
        antwort = await lifecycle.load([TTS])
        args, env = spawner.calls[0]
        self.assertEqual(args, ["bash", "/repo/runtime/qwen-tts/"
                                       "start-qwen-tts.sh"])
        self.assertEqual(env["QWEN_TTS_HOST"], "127.0.0.1")
        self.assertEqual(env["QWEN_TTS_PORT"], "30005")
        self.assertEqual(env["QWEN_TTS_MODEL_DIR"],
                         "/mnt/models/audio/experiments/qwen")
        self.assertEqual(env["QWEN_TTS_PIDFILE"],
                         "/repo/runtime/qwen-tts/qwen-tts.pid")
        self.assertIn("/load", self.qwen_tts.calls)
        self.assertEqual([e["profile"] for e in antwort["loaded"]], [TTS])
        self.assertEqual(antwort["loaded"][0]["kind"], "tts")

    async def test_load_qwen3_tts_bei_laufendem_chat_409(self):
        self.control.state = "running"
        self.control.backend = "qwen"
        spawner = FakeSpawner(on_spawn=self._qwen_start)
        lifecycle = self.lifecycle(spawner=spawner)
        with self.assertRaises(LifecycleConflictError):
            await lifecycle.load([TTS])
        self.assertEqual(spawner.calls, [])

    async def test_load_chat_bei_geladenem_qwen3_tts_409(self):
        self.qwen_tts.alive = True
        self.qwen_tts.model_loaded = True
        lifecycle = self.lifecycle()
        with self.assertRaises(LifecycleConflictError):
            await lifecycle.load([QWEN])
        self.assertEqual(self.control.started, [])

    async def test_load_chat_bei_laufendem_aber_nicht_geladenem_tts_409(self):
        self.qwen_tts.alive = True
        self.qwen_tts.model_loaded = False
        with self.assertRaises(LifecycleConflictError):
            await self.lifecycle().load([QWEN])
        self.assertEqual(self.control.started, [])

    async def test_load_qwen3_tts_oom_503(self):
        self.qwen_tts.load_status = 503
        lifecycle = self.lifecycle(spawner=FakeSpawner(
            on_spawn=self._qwen_start))
        with self.assertRaises(LifecycleUnavailableError):
            await lifecycle.load([TTS])

    async def test_load_audio8_startet_worker_und_laedt(self):
        spawner = FakeSpawner(on_spawn=self._audio8_start)
        lifecycle = self.lifecycle(spawner=spawner)
        antwort = await lifecycle.load([AUDIO8])
        args, env = spawner.calls[0]
        self.assertEqual(args, ["bash", "/repo/runtime/audio8/"
                                       "start-audio8.sh"])
        self.assertEqual(env["AUDIO8_HOST"], "127.0.0.1")
        self.assertEqual(env["AUDIO8_PORT"], "30006")
        self.assertEqual(env["AUDIO8_MODEL_DIR"], "/mnt/models/audio/audio8")
        self.assertEqual(env["AUDIO8_REFERENCE_AUDIO"],
                         "/mnt/models/audio/audio8/reference/de_male.wav")
        self.assertEqual(env["AUDIO8_REFERENCE_TEXT"],
                         "/mnt/models/audio/audio8/reference/de_male.txt")
        self.assertEqual(env["AUDIO8_PIDFILE"],
                         "/repo/runtime/audio8/audio8.pid")
        self.assertIn("/load", self.audio8.calls)
        self.assertEqual([e["profile"] for e in antwort["loaded"]], [AUDIO8])
        self.assertEqual(antwort["loaded"][0]["kind"], "tts")

    async def test_load_audio8_bei_laufendem_qwen3_tts_409(self):
        self.qwen_tts.alive = True
        self.qwen_tts.model_loaded = True
        spawner = FakeSpawner(on_spawn=self._audio8_start)
        lifecycle = self.lifecycle(spawner=spawner)
        with self.assertRaises(LifecycleConflictError):
            await lifecycle.load([AUDIO8])
        self.assertEqual(spawner.calls, [])

    async def test_load_audio8_bei_laufendem_aber_nicht_geladenem_qwen3_tts_409(self):
        self.qwen_tts.alive = True
        self.qwen_tts.model_loaded = False
        spawner = FakeSpawner(on_spawn=self._audio8_start)
        lifecycle = self.lifecycle(spawner=spawner)
        with self.assertRaises(LifecycleConflictError):
            await lifecycle.load([AUDIO8])
        self.assertEqual(spawner.calls, [])

    async def test_load_qwen3_tts_bei_laufendem_audio8_409(self):
        self.audio8.alive = True
        self.audio8.model_loaded = True
        spawner = FakeSpawner(on_spawn=self._qwen_start)
        lifecycle = self.lifecycle(spawner=spawner)
        with self.assertRaises(LifecycleConflictError):
            await lifecycle.load([TTS])
        self.assertEqual(spawner.calls, [])

    async def test_load_qwen3_tts_bei_laufendem_aber_nicht_geladenem_audio8_409(self):
        self.audio8.alive = True
        self.audio8.model_loaded = False
        spawner = FakeSpawner(on_spawn=self._qwen_start)
        lifecycle = self.lifecycle(spawner=spawner)
        with self.assertRaises(LifecycleConflictError):
            await lifecycle.load([TTS])
        self.assertEqual(spawner.calls, [])

    async def test_load_audio8_bei_laufendem_chat_409(self):
        self.control.state = "running"
        self.control.backend = "qwen"
        spawner = FakeSpawner(on_spawn=self._audio8_start)
        lifecycle = self.lifecycle(spawner=spawner)
        with self.assertRaises(LifecycleConflictError):
            await lifecycle.load([AUDIO8])
        self.assertEqual(spawner.calls, [])

    def _embed_bereit(self):
        self.embed.version = True


class TestUnload(LifecycleTestCase):
    async def test_all_entlaedt_qwen(self):
        self.control.state = "running"
        self.control.backend = "qwen"
        antwort = await self.lifecycle().unload()
        self.assertEqual(self.control.stopped, ["qwen"])
        self.assertEqual([p["profile"] for p in antwort["unloaded"]], [QWEN])
        self.assertEqual(antwort["loaded"], [])
        self.assertEqual(antwort["requested"], "all")

    async def test_all_entlaedt_ollama_modell_ohne_prozess_zu_stoppen(self):
        self.control.state = "running"
        self.control.backend = "flint"
        self.chat.models.add(FLINT)
        antwort = await self.lifecycle().unload()
        self.assertEqual(self.chat.body_for("/api/generate")[-1]["keep_alive"], 0)
        self.assertEqual(antwort["loaded"], [])
        self.assertEqual(self.control.stopped, [])
        self.assertEqual(self.control.state, "running")

    async def test_all_entlaedt_embedding(self):
        self.embed.models.add(EMBED)
        antwort = await self.lifecycle().unload()
        self.assertEqual(self.embed.body_for("/api/embed")[-1]["keep_alive"], 0)
        self.assertEqual([p["profile"] for p in antwort["unloaded"]], [EMBED])

    async def test_gezielt_entlaedt_nur_das_modell(self):
        self.control.state = "running"
        self.control.backend = "flint"
        self.chat.models.add(FLINT)
        self.embed.models.add(EMBED)
        antwort = await self.lifecycle().unload([FLINT])
        self.assertEqual(self.chat.body_for("/api/generate")[-1]["keep_alive"], 0)
        self.assertEqual(self.embed.body_for("/api/embed"), [])
        self.assertEqual([p["profile"] for p in antwort["unloaded"]], [FLINT])
        self.assertEqual([p["profile"] for p in antwort["loaded"]], [EMBED])

    async def test_inaktives_modell_ist_no_op(self):
        antwort = await self.lifecycle().unload([QWEN])
        self.assertEqual(self.control.stopped, [])
        self.assertEqual(antwort["unloaded"], [])
        self.assertEqual(antwort["loaded"], [])

    async def test_unbekannter_name_entlaedt_nichts(self):
        self.control.state = "running"
        self.control.backend = "qwen"
        with self.assertRaises(UnknownProfileError):
            await self.lifecycle().unload([QWEN, "gibt-es-nicht"])
        self.assertEqual(self.control.stopped, [])

    async def test_deaktivierte_steuerung_verweigert_aktives_chat_modell(self):
        self.config = Config(db_path="x.db", server_control_enabled=False)
        self.control.state = "running"
        self.control.backend = "qwen"
        with self.assertRaises(LifecycleDisabledError):
            await self.lifecycle().unload()
        self.assertEqual(self.control.stopped, [])

    async def test_unload_qwen3_tts_stoppt_worker(self):
        self.qwen_tts.alive = True
        self.qwen_tts.model_loaded = True
        spawner = FakeSpawner(on_spawn=self._qwen_stop)
        lifecycle = self.lifecycle(spawner=spawner)
        antwort = await lifecycle.unload([TTS])
        self.assertIn("/unload", self.qwen_tts.calls)
        self.assertEqual(spawner.calls[0][0],
                         ["bash", "/repo/runtime/qwen-tts/stop-qwen-tts.sh"])
        self.assertEqual([e["profile"] for e in antwort["unloaded"]], [TTS])
        self.assertEqual(antwort["loaded"], [])

    async def test_unload_audio8_stoppt_worker(self):
        self.audio8.alive = True
        self.audio8.model_loaded = True
        spawner = FakeSpawner(on_spawn=self._audio8_stop)
        lifecycle = self.lifecycle(spawner=spawner)
        antwort = await lifecycle.unload([AUDIO8])
        self.assertIn("/unload", self.audio8.calls)
        self.assertEqual(spawner.calls[0][0],
                         ["bash", "/repo/runtime/audio8/stop-audio8.sh"])
        self.assertEqual([e["profile"] for e in antwort["unloaded"]], [AUDIO8])
        self.assertEqual(antwort["loaded"], [])

    async def test_unload_audio8_laesst_qwen3_tts_unberuehrt(self):
        self.qwen_tts.alive = True
        self.qwen_tts.model_loaded = True
        self.audio8.alive = True
        self.audio8.model_loaded = True
        spawner = FakeSpawner(on_spawn=self._audio8_stop)
        lifecycle = self.lifecycle(spawner=spawner)
        antwort = await lifecycle.unload([AUDIO8])
        self.assertNotIn("/unload", self.qwen_tts.calls)
        self.assertTrue(self.qwen_tts.alive)
        self.assertEqual([e["profile"] for e in antwort["unloaded"]], [AUDIO8])
        self.assertEqual([e["profile"] for e in antwort["loaded"]], [TTS])

    async def test_unload_qwen3_tts_stoppt_laufenden_aber_nicht_geladenen_worker(
            self):
        self.qwen_tts.alive = True
        self.qwen_tts.model_loaded = False
        spawner = FakeSpawner(on_spawn=self._qwen_stop)
        antwort = await self.lifecycle(spawner=spawner).unload([TTS])
        self.assertIn("/unload", self.qwen_tts.calls)
        self.assertEqual(spawner.calls[0][0],
                         ["bash", "/repo/runtime/qwen-tts/stop-qwen-tts.sh"])
        self.assertEqual([e["profile"] for e in antwort["unloaded"]], [TTS])

    async def test_unload_audio8_stoppt_laufenden_aber_nicht_geladenen_worker(
            self):
        self.audio8.alive = True
        self.audio8.model_loaded = False
        spawner = FakeSpawner(on_spawn=self._audio8_stop)
        antwort = await self.lifecycle(spawner=spawner).unload([AUDIO8])
        self.assertIn("/unload", self.audio8.calls)
        self.assertEqual(spawner.calls[0][0],
                         ["bash", "/repo/runtime/audio8/stop-audio8.sh"])
        self.assertEqual([e["profile"] for e in antwort["unloaded"]], [AUDIO8])

    async def test_unload_all_stoppt_beide_tts_worker(self):
        self.qwen_tts.alive = True
        self.qwen_tts.model_loaded = True
        self.audio8.alive = True
        self.audio8.model_loaded = True

        def stop_beide():
            if self.qwen_tts.alive:
                self.qwen_tts.alive = False
            elif self.audio8.alive:
                self.audio8.alive = False

        spawner = FakeSpawner(on_spawn=stop_beide)
        antwort = await self.lifecycle(spawner=spawner).unload()
        gestoppt = {call[0][1] for call in spawner.calls}
        self.assertIn("/repo/runtime/qwen-tts/stop-qwen-tts.sh", gestoppt)
        self.assertIn("/repo/runtime/audio8/stop-audio8.sh", gestoppt)
        self.assertEqual({e["profile"] for e in antwort["unloaded"]},
                         {TTS, AUDIO8})


if __name__ == "__main__":
    unittest.main()
