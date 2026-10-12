"""FastAPI-App: Zustand als JSON und als SSE-Stream, dazu die HTML-Seite.

Der Takt gehoert dem Server, nicht dem Browser
----------------------------------------------
``collector.tick()`` liest Datei, ``/get_load``, ``/metrics`` und ``nvidia-smi``
- er ist blockierend und laeuft ueber ``asyncio.to_thread``. Ein Tick **pro
SSE-Client** hiesse: kein Browser, kein Poll, keine Historie und keine
Log-Rotation. Die Schutzschicht gegen unbegrenztes Logwachstum (``truncate -s 0``
ab 1 GB, Spec Z. 321-341) ruhte dann genau dann, wenn sie gebraucht wird - und
der Server soll doch dauerhaft laufen (Spec Z. 371-374).

Deshalb startet der **Lifespan der App** genau einmal einen Ticker-Task. Der
SSE-Generator liest danach nur noch ``collector.state()``: zwei Browser sind
zwei Leser, ein Ticker. Ein Fehler im Ticker wird protokolliert, der naechste
Poll laeuft normal weiter - ein Fehler im Monitor darf nie als Signal
erscheinen.
"""

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, StreamingResponse

HERE = os.path.dirname(os.path.abspath(__file__))


def log(message):
    """Eine Zeile fuer das Monitor-Log, mit demselben Format wie `Collector._log`.

    Der Collector schreibt ueber seinen eigenen Kanal, weil er den Pfad des
    Monitor-Logs nicht kennt - er gibt die Zeile auf `stderr` aus, und das
    Startskript leitet das nach `MONITOR_LOG` um. Der Server hat es mit dem
    Collector-Aufbau nicht anders, benutzt aber keine private Methode einer
    Fremdklasse.
    """
    print(f"[slotmon] {time.strftime('%Y-%m-%d %H:%M:%S')} {message}",
          file=sys.stderr, flush=True)


async def tick_forever(collector):
    """Endlos: alle ``poll_interval`` ein ``collector.tick()``, im Thread.

    Der erste Poll laeuft **sofort**. Ein Warte-Takt beim Start waere ein
    Zeitfenster, in dem der Slot-Zustand leer ist, obwohl die Quelle laeuft -
    und genau in diesem Fenster zeigt das Dashboard den Zustand vor dem ersten
    Messwert.

    Der Fehlerkanal ist bewusst derselbe wie im SSE-Generator: protokollieren,
    weiterschlafen. Ein ``tick()``, das zweimal hintereinander wirft, darf den
    Monitor nicht beenden - und darf auch nicht den Takt verdoppeln.
    """
    while True:
        try:
            await asyncio.to_thread(collector.tick)
        except asyncio.CancelledError:
            # Herunterfahren. Der `finally` im Lifespan wartet auf dieses
            # Cancelled; ein Verschlucken wuerde den Task ewig laufen lassen.
            raise
        except Exception as exc:  # noqa: BLE001 - siehe Docstring
            log(f"tick-error {type(exc).__name__}: {exc}")
        await asyncio.sleep(collector.cfg.poll_interval)


def create_app(collector):
    @asynccontextmanager
    async def lifespan(app):
        ticker = asyncio.create_task(tick_forever(collector), name="slotmon-ticker")
        # Fuer den Test sichtbar: der Task muss beim Herunterfahren beendet
        # sein, und ein Test, der ihn nicht wegraeumt, blockiert die Schleife
        # bis zum Prozessende.
        app.state.ticker = ticker
        try:
            yield
        finally:
            # Sauber beenden, nicht den Prozess killen. `cancel()` hebt den
            # laufenden Poll ab, das `await` darunter wartet, bis der Task
            # wirklich fertig ist - sonst wartet der naechste Start auf einen
            # Prozess, der noch am Zaehlen ist.
            ticker.cancel()
            with suppress(asyncio.CancelledError):
                await ticker

    app = FastAPI(title="slotmon", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.get("/api/state")
    def state():
        return collector.state()

    @app.get("/api/stream")
    async def stream():
        async def events():
            while True:
                try:
                    payload = collector.state()
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                except Exception as exc:  # der Monitor darf daran nicht sterben
                    error = {"error": f"{type(exc).__name__}: {exc}"}
                    yield f"data: {json.dumps(error)}\n\n"
                await asyncio.sleep(collector.cfg.poll_interval)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/", response_class=HTMLResponse)
    def index():
        with open(os.path.join(HERE, "dashboard.html"), encoding="utf-8") as handle:
            return Response(handle.read(), media_type="text/html; charset=utf-8")

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


def main():
    import uvicorn

    from slotmon.collector import Collector
    from slotmon.config import load_config
    from slotmon.names import load_names

    cfg = load_config()
    names = load_names(cfg.systems_file)
    app = create_app(Collector(cfg, names))
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()