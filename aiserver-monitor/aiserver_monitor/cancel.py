"""Abbruch am SGLang-Upstream: ``POST {upstream_base}/abort_request``.

Kapselt nur den HTTP-Aufruf; Client und Basis-URL kommen von aussen herein
(kein eigener Client, keine Config). Ein Einzelabbruch verlangt eine
nicht-leere rid: SGLang macht aus ``None`` die leere Zeichenkette, und ein
``startswith("")`` auf Serverseite wuerde dann alle Anfragen treffen. Darum
gibt es bei ``abort_all=False`` ohne Inhalt sofort einen ``ValueError``,
ohne dass ein HTTP-Aufruf rausgeht.
"""

import logging

import httpx

logger = logging.getLogger(__name__)


async def abort_request(client: httpx.AsyncClient, upstream_base: str, *,
                        rid: str | None, abort_all: bool) -> bool:
    """Bricht einen Request (``rid``) oder alle (``abort_all``) ab.

    Liefert ``True`` nur bei HTTP 200; andere Status werden geloggt und
    liefern ``False``. Doppelter Aufruf ist kein Fehler (idempotent).
    """
    if not abort_all and not rid:
        raise ValueError("Einzelabbruch braucht eine nicht-leere rid")
    response = await client.post(
        f"{upstream_base}/abort_request",
        json={"rid": rid or "", "abort_all": abort_all},
    )
    if response.status_code == 200:
        return True
    logger.warning("Abort abgelehnt (HTTP %s): rid=%r abort_all=%s",
                   response.status_code, rid, abort_all)
    return False
