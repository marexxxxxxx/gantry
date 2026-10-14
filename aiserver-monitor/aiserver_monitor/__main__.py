"""Einstiegspunkt: ``python -m aiserver_monitor`` startet das Gateway.

Liest optional eine TOML-Datei aus ``AISERVER_MONITOR_CONFIG`` (leer oder
nicht gesetzt = keine Datei; die uebrigen Felder kommen wie gewohnt aus
``AISERVER_MONITOR_<FELD>``), richtet das rotierende Dateilogging unter
``config.log_path`` ein und startet uvicorn mit genau einem Worker.
``log_config=None`` laesst uvicorn die Root-Logger-Konfiguration benutzen -
so landen auch uvicorn-Meldungen in der rotierenden Datei.
"""

import logging
import logging.handlers
import os
from pathlib import Path

import uvicorn

from .app import create_app
from .config import load_config


def main() -> None:
    pfad = os.environ.get("AISERVER_MONITOR_CONFIG") or None
    config = load_config(pfad)

    Path(config.log_path).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.handlers.RotatingFileHandler(
            config.log_path, maxBytes=5_000_000, backupCount=3,
            encoding="utf-8")],
    )

    # Genau ein Worker: SQLite-History, Job-Registry und der SSE-Hub liegen im
    # Prozess; ein zweiter Worker haette einen zweiten, unabhaengigen Zustand.
    uvicorn.run(create_app(config), host=config.listen_host,
                port=config.listen_port, log_config=None)


if __name__ == "__main__":
    main()
