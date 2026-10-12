"""Der EINE schreibende Zugriff des Monitors.

Alles andere im Monitor liest. Nur dieses Modul schreibt - und nur auf eine
Weise: ein einzelner `POST /abort_request` mit **genau einer** `rid`.

Das ist keine Vorsicht, sondern eine Grenze, die man pruefen kann:
`tests/test_no_socket_source.py` laesst genau diese Datei zu.

Warum ueberhaupt geschrieben wird
---------------------------------
Der Nutzer hat es entschieden. Die Ueberlegung steht in der Spec (Abschnitt
"Die Randbedingung, die sich aendert"); das Entscheidende ist hier die
Einschraenkung: **immer genau ein Auftrag, nie die Warteschlange.**

Die zwei Fallen im Server
-------------------------
1. `io_struct.py:1541` macht aus `rid=None` die leere Zeichenkette.
2. `scheduler.py:2814` vergleicht mit `req.rid.startswith(recv_req.rid)`,
   und `"x".startswith("")` ist wahr.

Gemeinsam: eine leere rid loescht die **ganze Warteschlange**, ohne dass
`abort_all` gesetzt waere. Der Server hat eine eigene Verteidigung
(`rid_to_state`-Guard im TokenizerManager, `tokenizer_manager.py:1343`), die
greift. Wir bauen trotzdem eine eigene und testen den **erzeugten Request** -
eine fremde Verteidigung ist keine eigene.
"""

import httpx

ABORT_PATH = "/abort_request"

#: Sekunden, nach denen der Monitor aufgibt. Der Server haengt bei einem
#: ausgelasteten Scheduler nicht lange, aber "haengt nicht" ist kein Timeout.
TIMEOUT_S = 10.0


def _cfg(cfg):
    return {
        "api": getattr(cfg, "api", "http://127.0.0.1:30000"),
        "auth": getattr(cfg, "auth", "Bearer marec"),
    }


def _nein(reason, rid=None):
    return {"aborted": False, "rid": rid, "reason": reason}


def _hole_holder(state):
    """Die rid des laufenden Auftrags, oder `None` mit einem Grund."""
    slot = (state or {}).get("slot") or {}
    if slot.get("state") != "busy":
        return None, "Kein Auftrag laeuft - nichts zu tun"
    rid = slot.get("rid")
    if not isinstance(rid, str) or not rid:
        return None, "Kein Auftrag laeuft - keine rid"
    if slot.get("legacy"):
        return None, f"Halter nicht eindeutig (Altbestand {slot['legacy']})"
    reason = slot.get("reason")
    if isinstance(reason, str) and reason:
        return None, reason
    return rid, None


def _hole_wartenden(state, position):
    """Die rid an gegebener Position, oder `None` mit einem Grund."""
    slot = (state or {}).get("slot") or {}
    if slot.get("legacy"):
        return None, f"Warteschlangen nicht ableitbar (Altbestand {slot['legacy']})"
    liste = ((state or {}).get("queue") or {}).get("waiting_list") or []
    if not isinstance(liste, list):
        return None, "Warteschlange unbekannt"
    if not isinstance(position, int) or isinstance(position, bool):
        return None, f"Position {position!r} ist keine Zahl"
    if position < 1:
        return None, f"Position {position} gibt es nicht"
    if position > len(liste):
        return None, f"Position {position} gibt es nicht (nur {len(liste)})"
    eintrag = liste[position - 1]
    rid = (eintrag or {}).get("rid")
    if not isinstance(rid, str) or not rid:
        return None, f"Position {position} traegt keine rid"
    return rid, None


def _sende(cfg, rid):
    """Der POST. Genau einer, mit genau einer rid, ohne `abort_all`.

    Der `abort_all`-Schluessel fehlt **absichtlich** im Body: `AbortReq` hat
    einen Default `False`, und ein zusaetzliches Feld, das jemand auf `True`
    setzen kann, ist ein Feld, das jemand auf `True` setzt.
    """
    werte = _cfg(cfg)
    try:
        antwort = httpx.post(
            werte["api"] + ABORT_PATH,
            json={"rid": rid},
            headers={"Authorization": werte["auth"]},
            timeout=TIMEOUT_S,
        )
    except httpx.TimeoutException:
        return _nein("Server antwortet nicht (Zeitueberschreitung)")
    except httpx.HTTPError as exc:
        return _nein(f"Server nicht erreichbar: {type(exc).__name__}")
    if antwort.status_code != 200:
        return _nein(f"Server lehnt ab (HTTP {antwort.status_code})")
    return {"aborted": True, "rid": rid, "reason": "abgebrochen"}


def abort_holder(cfg, state):
    """Bricht **den laufenden** Auftrag ab. Kein Parameter, keine Liste."""
    rid, grund = _hole_holder(state)
    if rid is None:
        return _nein(grund)
    return _sende(cfg, rid)


def abort_waiting(cfg, state, position):
    """Bricht **den Wartenden an `position`** ab.

    Der Nutzer gibt die Zahl an, die er im Dashboard sieht. Die rid loest
    der Monitor im Klickmoment auf - damit kann keine rid verwechselt
    werden, und die Liste kann sich zwischen Anzeige und Klick verschieben,
    ohne dass eine falsche rid getroffen wird.
    """
    rid, grund = _hole_wartenden(state, position)
    if rid is None:
        return _nein(grund)
    return _sende(cfg, rid)
