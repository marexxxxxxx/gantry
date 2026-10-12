"""FastAPI-App: Zustand als JSON und als SSE-Stream, dazu die HTML-Seite."""

import asyncio
import json
import os

from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, StreamingResponse

HERE = os.path.dirname(os.path.abspath(__file__))


def create_app(collector):
    app = FastAPI(title="slotmon", docs_url=None, redoc_url=None)

    @app.get("/api/state")
    def state():
        return collector.state()

    @app.get("/api/stream")
    async def stream():
        async def events():
            while True:
                try:
                    payload = await asyncio.to_thread(collector.tick)
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