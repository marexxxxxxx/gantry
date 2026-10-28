"""Metrik-Sammler: SGLang-Metriken oder Flint-Werte plus GPU je ``poll_interval_s``.

Der Sammler laeuft als Hintergrundtask der App: :meth:`Collector.run` holt je
Durchlauf die Werte des Upstreams, liest die GPU ueber ``config.gpu_cmd``
(oder einen injizierten ``gpu_reader``), baut das Sample-Dict und meldet es an
die History und den SSE-Hub (Ereignis ``stats``).

Zwei Backends: SGLang liefert ``/metrics`` + ``/health`` (Prometheus); Flint
(Ollama) hat beides nicht, antwortet aber auf ``/api/version``. Dann kommen
Modell/Kontext aus ``/api/ps``, laufende/wartende Anfragen und der Durchsatz
aus der Job-Registry des Gateways. Ist der Upstream weg, entsteht trotzdem
ein Sample mit ``available=False``; nur gemessene Werte stehen im Sample
("nicht gemessen" heisst fehlender Key, nie 0).
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import httpx

from .config import Config
from .metrics_parse import (
    GpuSample,
    extract_flint_sample,
    extract_sample,
    parse_nvidia_smi,
    parse_prometheus,
)
from .proxy import Deps

logger = logging.getLogger(__name__)

#: Gesamt-Zeitlimit je Upstream-Aufruf: ein haengender Upstream darf die
#: Sammelschleife nicht blockieren. Bewusst kein Config-Feld.
_HTTP_TIMEOUT_S = 5.0

#: Zeitlimit des nvidia-smi-Aufrufs: ein haengendes Kommando darf den
#: Messdurchlauf nicht blockieren; danach wird es beendet (best effort).
_GPU_TIMEOUT_S = 5.0

#: Wartezeit nach dem Kill, damit der Prozess eingesammelt wird. Laeuft sie
#: ab (z. B. haelt ein Wrapper-Skript einen Enkelprozess), kehrt der
#: Messdurchlauf trotzdem zurueck; der Child-Watcher raeumt spaeter auf.
_GPU_KILL_WARTEZEIT_S = 1.0

#: Abstand der periodischen Retention in ``run()``: haeufiger als stuendlich
#: bringt nichts (Stichtage sind Tage) und wuerde nur Last erzeugen.
_CLEANUP_ABSTAND_S = 3600.0

#: Argumente der nvidia-smi-Abfrage: Auslastung, VRAM belegt und gesamt.
_GPU_ARGUMENTE = (
    "--query-gpu=utilization.gpu,memory.used,memory.total",
    "--format=csv,noheader,nounits",
)

#: Flint (Ollama) hat kein ``/metrics``; ``/api/ps`` liefert Modell und
#: Kontextgroesse, ``/api/version`` ist der Erkennungs-Endpunkt.
_FLINT_PS_PATH = "/api/ps"

#: Zeitfenster fuer die Durchsatz-Schaetzung aus fertigen Jobs: nur was
#: gerade eben fertig wurde, zaehlt als "aktueller" Durchsatz.
_FLINT_DURCHSATZ_FENSTER_S = 5.0


async def _nvidia_smi_lesen(config: Config) -> GpuSample:
    """GPU-Messwerte per ``config.gpu_cmd``; jeder Fehler wird zu None-Werten.

    Fehlt das Kommando, endet es ungleich 0, laeuft es ins Leere oder ist die
    Ausgabe nicht dekodierbar, bleibt das GpuSample leer ("nicht gemessen");
    der Reader laesst nie eine Exception raus, sondern loggt das Problem nur.
    """
    try:
        prozess = await asyncio.create_subprocess_exec(
            config.gpu_cmd, *_GPU_ARGUMENTE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(prozess.communicate(),
                                           timeout=_GPU_TIMEOUT_S)
    except TimeoutError:
        logger.warning("nvidia-smi-Timeout nach %.1f s", _GPU_TIMEOUT_S)
        try:
            prozess.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(prozess.wait(),
                                   timeout=_GPU_KILL_WARTEZEIT_S)
        except TimeoutError:
            logger.warning("nvidia-smi-Prozess %s nicht eingesammelt",
                           prozess.pid)
        return GpuSample(None, None, None)
    except Exception as exc:
        logger.warning("nvidia-smi-Aufruf fehlgeschlagen: %s: %s",
                       type(exc).__name__, exc)
        return GpuSample(None, None, None)
    if prozess.returncode != 0:
        logger.warning("nvidia-smi endet mit Status %s", prozess.returncode)
        return GpuSample(None, None, None)
    try:
        ausgabe = stdout.decode()
    except UnicodeDecodeError as exc:
        logger.warning("nvidia-smi-Ausgabe nicht dekodierbar: %s", exc)
        return GpuSample(None, None, None)
    return parse_nvidia_smi(ausgabe)


class Collector:
    """Metrik-Sammler: ein Durchlauf je Aufruf, ``run`` als Endlosschleife.

    ``deps`` liefert Config, History und Hub; der httpx-Client kommt aus
    ``deps.client`` oder als eigener ``client`` (eigener Pool, damit volle
    Proxy-Pools die Messung nicht blockieren). Ein injizierter
    ``gpu_reader`` ersetzt den nvidia-smi-Aufruf vollstaendig; ohne ihn liest
    :func:`_nvidia_smi_lesen` die GPU.
    """

    def __init__(self, deps: Deps, *,
                 gpu_reader: Callable[[], Awaitable[GpuSample]] | None = None,
                 client: httpx.AsyncClient | None = None):
        self._deps = deps
        self._gpu_reader = gpu_reader
        self._client = deps.client if client is None else client

    async def _gpu_lesen(self) -> GpuSample:
        """GPU-Messwerte: injizierter Reader oder ``config.gpu_cmd``."""
        if self._gpu_reader is not None:
            return await self._gpu_reader()
        return await _nvidia_smi_lesen(self._deps.config)

    async def _upstream_werte(self) -> tuple[dict[str, float], bool, dict | None]:
        """Holt SGLang-Metriken oder Flint-Daten.

        Rueckgabe ``(werte, available, flint)``: Bei SGLang sind ``werte`` die
        Prometheus-Summen und ``flint`` None. Antwortet ``/metrics`` oder
        ``/health`` nicht mit 200, wird ``/api/version`` probiert: 200 heisst
        Flint (Ollama), dann kommen die Werte aus :meth:`_flint_werte`.
        Scheitert beides, ist ``available`` False und alle Messwerte fehlen.
        """
        basis = self._deps.config.upstream_base.rstrip("/")
        try:
            metrik_antwort = await self._client.get(
                f"{basis}/metrics", timeout=_HTTP_TIMEOUT_S)
            health_antwort = await self._client.get(
                f"{basis}/health", timeout=_HTTP_TIMEOUT_S)
        except Exception as exc:
            logger.warning("Upstream nicht erreichbar: %s: %s",
                           type(exc).__name__, exc)
        else:
            if (metrik_antwort.status_code == 200
                    and health_antwort.status_code == 200):
                return parse_prometheus(metrik_antwort.text), True, None
            logger.debug("SGLang-Metriken fehlen (metrics=%s health=%s)",
                         metrik_antwort.status_code,
                         health_antwort.status_code)

        flint = await self._flint_werte()
        if flint is not None:
            return {}, True, flint
        logger.warning("Upstream weder SGLang noch Flint erreichbar")
        return {}, False, None

    async def _flint_werte(self) -> dict | None:
        """Flint-Daten aus ``/api/version`` und ``/api/ps``; None = kein Flint.

        ``/api/version`` ist der Erkennungs-Endpunkt (SGLang: 404). ``/api/ps``
        liefert die Kontextgroesse des geladenen Modells; fehlt sie, bleibt
        ``max_tokens`` weg ("nicht gemessen", nie 0).
        """
        basis = self._deps.config.upstream_base.rstrip("/")
        try:
            version = await self._client.get(
                f"{basis}{self._deps.config.flint_health_path}",
                timeout=_HTTP_TIMEOUT_S)
        except Exception as exc:
            logger.debug("Flint-Probe fehlgeschlagen: %s: %s",
                         type(exc).__name__, exc)
            return None
        if version.status_code != 200:
            return None

        werte: dict = {}
        try:
            ps = await self._client.get(
                f"{basis}{_FLINT_PS_PATH}", timeout=_HTTP_TIMEOUT_S)
            daten = ps.json() if ps.status_code == 200 else None
        except Exception as exc:
            logger.debug("/api/ps nicht lesbar: %s: %s",
                         type(exc).__name__, exc)
            daten = None
        modelle = daten.get("models") if isinstance(daten, dict) else None
        if isinstance(modelle, list) and modelle:
            modell = modelle[0]
            if isinstance(modell, dict):
                kontext = modell.get("context_length")
                if isinstance(kontext, int) and kontext > 0:
                    werte["max_tokens"] = kontext
        return werte

    def _job_werte(self) -> dict:
        """Laufende/wartende Jobs und frischer Durchsatz aus der Registry.

        Flint meldet keine eigenen Zaehler; das Gateway kennt seine Jobs.
        ``throughput`` ist die Summe der tok/s von Jobs, die innerhalb des
        Fensters fertig wurden (bei np=2 also beide Slots zusammen).
        """
        jetzt = time.time()
        werte = {"running": 0, "waiting": 0}
        durchsatz: float | None = None
        for job in self._deps.registry.snapshot():
            status = job.get("status")
            if status == "running":
                werte["running"] += 1
            elif status == "waiting":
                werte["waiting"] += 1
            rate = job.get("tok_per_s")
            fertig = job.get("finished_at_ms")
            if (status == "done" and rate and fertig is not None
                    and jetzt - fertig / 1000.0 <= _FLINT_DURCHSATZ_FENSTER_S):
                durchsatz = (durchsatz or 0.0) + float(rate)
        if durchsatz is not None:
            werte["throughput"] = round(durchsatz, 3)
        return werte

    async def sample_once(self) -> dict:
        """Ein Durchlauf: Upstream und GPU lesen, Sample melden, zurueckgeben.

        Erst die Upstream-Werte (Netz, zeitlich begrenzt), dann die GPU; das
        Sample geht an ``history.record_sample`` (falls History vorhanden) und
        als Ereignis ``stats`` an den Hub. Zurueck kommt genau dieses dict.
        """
        ts = time.time()
        parsed, available, flint = await self._upstream_werte()
        gpu = await self._gpu_lesen()
        if flint is not None:
            werte = {**flint, **self._job_werte()}
            sample = extract_flint_sample(werte, gpu, ts)
        else:
            sample = extract_sample(parsed, gpu, ts, available)
        if self._deps.history is not None:
            self._deps.history.record_sample(sample)
        self._deps.hub.publish("stats", sample)
        return sample

    async def run(self) -> None:
        """Endlosschleife: sofort messen, dann je ``poll_interval_s``.

        Zusaetzlich laeuft die Retention: hoechstens alle
        ``_CLEANUP_ABSTAND_S`` raeumt ``history.cleanup`` (nur mit History)
        alte Jobs und Samples weg; die geloeschten Zeilen stehen im Log
        (INFO). Ein Fehler in einem Durchlauf (Messen oder Aufraeumen) wird
        geloggt und beendet die Schleife nicht; ``asyncio.CancelledError``
        (App-Shutdown) laeuft bewusst nach oben durch.
        """
        letzter_cleanup = time.monotonic()
        while True:
            try:
                await self.sample_once()
                jetzt = time.monotonic()
                if (self._deps.history is not None
                        and jetzt - letzter_cleanup >= _CLEANUP_ABSTAND_S):
                    jobs_weg, samples_weg = self._deps.history.cleanup(
                        time.time())
                    letzter_cleanup = jetzt
                    logger.info("Retention: %s Jobs und %s Samples geloescht",
                                jobs_weg, samples_weg)
            except Exception:
                logger.warning("Messdurchlauf fehlgeschlagen", exc_info=True)
            await asyncio.sleep(self._deps.config.poll_interval_s)
