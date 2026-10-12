# Plan: Warteschlange und Auftragsabbruch

Basis: `0fabd20`, Branch `single-flight-serial-serving`, Arbeitsbaum sauber,
**408 Tests grün**.

Spec: `docs/superpowers/specs/2026-10-12-terminal-und-autostart-design.md`

## Goal

Das Dashboard zeigt die **Warteschlange in der Reihenfolge**, in der der Server
abarbeitet, und erlaubt das **Abbrechen eines einzelnen Auftrags** — des laufenden
Halters oder eines Wartenden an gegebener Position.

Der Nutzer hat die Reihenfolge der Priorität gesetzt: **zuerst das Abbrechen im
Dashboard**, danach die Terminal-Ansicht und der Autostart (eigener Plan).

## Architecture

```
slotmon/queue.py     NEU  offene rids, sortiert, mit Namens-Rückfall. Rein.
slotmon/abort.py     NEU  der EINE schreibende Zugriff: HTTP gegen sglang
slotmon/collector.py +1  führt queue im Zustand mit, wertet finish_reason aus
slotmon/server.py    +2  POST /api/abort--holder, POST /api/abort-waiting
slotmon/dashboard.html +1 Warteschlangenliste, Bestätigungsfenster, Löschknöpfe
tests/test_queue.py  NEU
tests/test_abort.py  NEU
```

Datenfluss eines Abbruchs:

```
Browser  POST /api/abort-waiting {"position": 3}
   ↓  server.py liest den *aktuellen* Zustand
collector.state() -> queue.waiting_list[2].rid
   ↓  abort.py: 8 Sicherheitsprüfungen, dann genau ein POST
sglang POST /abort_request {"rid": "...", "abort_all": false}
   ↓
nächster Poll: request.finished mit finish_reason=abort
   ↓
collector: eigener Ereigniseintrag, nicht in der Dauer-Mittelung
   ↓
Dashboard zeigt „abgebrochen" in der Zeile
```

## Tech Stack

Bestehendes: Python 3.12 (stdlib), FastAPI/uvicorn/httpx, `unittest`, kein pytest.
Für den Abbruch: **`httpx.post`** — dieselbe Bibliothek, die `sources.py` für die
GETs benutzt. Kein neues Paket.

## Global Constraints

1. **Nur `abort.py` schreibt je zum Server.** Alles andere bleibt lesend. Der Test
   `tests/test_no_socket_source.py` wird darauf erweitert.
2. **`abort_all` wird nie gesetzt.** Und eine **leere `rid` wird nie gesendet** —
   beides wird am *erzeugten Request* geprüft, nicht an der Absicht. Grund:
   `io_struct.py:1541` macht aus `None` die leere Zeichenkette, und
   `scheduler.py:2814` vergleicht mit `startswith`, wobei `"x".startswith("")`
   wahr ist.
3. **Kein Abbruch bei `legacy > 0`.** Solange ein Altbestand liegt, ist die Liste
   nicht ableitbar.
4. **`abort--holder` hat keinen Parameter.** Kein `?rid=`, kein `rid` im Body.
5. **Ein Abbruch pro Klick, immer genau einer.** Kein Sammelbefehl.
6. **Kein Prompt-Text**, weder im Dashboard noch im JSON.
7. **Nicht gemessen heißt `null`/`–`, nie `0`.**
8. **Ein wartender Auftrag trägt keinen Namen**, bis er fertig ist. Der Name
   erscheint nur, wenn dieselbe `ip_port` aus einem früheren Abschluss bekannt ist.
9. **Deutsch, ASCII-only** in Code, Kommentaren und Tests. Ausnahmen: der zitierte
   sglang-Code in Kommentaren und Test-Fixtures, die Mehr-Byte-Zeichen brauchen.
10. Der laufende sglang-Server wird **nicht angefasst** — auch nicht für einen
    Test. `abort.py` wird gegen `httpx` gemockt, wie `tests/test_sources.py`.

## Technische Grundlagen, die gemessen sind

- Offene `request.received` im Log: **35**, `num_reqs`: **35**,
  `num_waiting_reqs`: **34**. Die Liste ist damit belegt vollständig.
- `state["open"]` (logreader.py:457) speichert `rid -> event.at`, und `at` ist die
  Unix-Zeit aus `timestamp` **mit Mikrosekunden**. 214 Zeilen geprüft, **0
  Kollisionen**. Die Reihenfolge braucht also **kein** `received_time` und keine
  Änderung am Logreader.
- Die Access-Zeile wird **erst bei `finished`** verbraucht (`logreader.py:359`,
  `_attach_ip`). Sie darf **nicht** vorgezogen werden, sonst verliert der Halter
  seinen Namen.
- `_flush_finished` schreibt jeden Abschluss. `finish_reason` wird gelesen
  (`collector.py:313`), aber nirgends ausgewertet.
- `scheduler.py:2811` poppt einen Wartenden direkt aus der `waiting_queue` — der
  Abbruch eines Wartenden kostet **keine Rechenzeit**.

---

# Task 1: Die Warteschlangenliste

**Files:**
- Create: `slotmon/queue.py`
- Create: `tests/test_queue.py`

**Interfaces:**
- Consumes: `open_rids: dict[rid -> Unix-Zeit]`, `waiting: int | None`,
  `legacy: int | None`, `names: dict`, `known_ips: dict[ip_port -> system]`
- Produces: `slotmon.queue.build(open_rids, waiting, legacy, resolve) -> list[dict]`

**Was die Liste enthält, je Eintrag:**

| Feld | Quelle | Bedeutung |
|---|---|---|
| `position` | Index + 1 | 1 = als nächster dran |
| `rid` | aus `open_rids` | die Kennung des Servers |
| `waiting_since` | der Unix-Zeit aus `open_rids` | Warten seit |
| `system` | `known_ips` über `resolve` | oder `None` |
| `known` | `system is not None` | steuert die Anzeige |

## Schritt 1: Der rote Test

```python
# tests/test_queue.py
"""Die Warteschlange in der Reihenfolge, in der der Server abarbeitet.

Belegt durch Messung vom 2026-10-12: 35 offene `request.received` im Log,
`num_reqs: 35`, `num_waiting_reqs: 34`. Die Liste ist damit vollstaendig -
die alte Aussage "Reihenfolge nicht ableitbar" galt dem Zustand vor dem
Aktivieren des Request-Loggings.
"""

import unittest

from slotmon.queue import build


class TestOrder(unittest.TestCase):
    def test_oldest_comes_first(self):
        """Position 1 ist der naechste dran.

        Der Server arbeitet FIFO ab. Der aelteste `request.received` ist
        deshalb der naechste - nicht der neueste, und nicht irgendeiner.
        """
        offene = {"bbb": 300.0, "aaa": 100.0, "ccc": 200.0}
        liste = build(offene, waiting=3, legacy=0, resolve=lambda ip: None)
        self.assertEqual([e["rid"] for e in liste], ["aaa", "ccc", "bbb"])
        self.assertEqual([e["position"] for e in liste], [1, 2, 3])

    def test_order_survives_microsecond_timestamps(self):
        """Zwei rids im selben Takt behalten die Eingangsreihenfolge.

        Unix-Zeit traegt Mikrosekunden, aber das ist keine Garantie. Ohne
        den Tie-Break waere die Reihenfolge von der Python-Implementierung
        von dict abhaengig.
        """
        offene = {"bbb": 100.0, "aaa": 100.0, "ccc": 100.0}
        liste = build(offene, waiting=3, legacy=0, resolve=lambda ip: None)
        self.assertEqual([e["rid"] for e in liste], ["aaa", "bbb", "ccc"])

    def test_seconds_stay_out_of_the_ordering(self):
        """Der Zeitstempel wird nicht in Sekunden gerundet fuer die Reihenfolge."""
        offene = {"a": 100.0, "b": 100.4, "c": 100.9}
        liste = build(offene, waiting=3, legacy=0, resolve=lambda ip: None)
        self.assertEqual([e["rid"] for e in liste], ["a", "b", "c"])
```

Führe aus: `python3.12 -B -m unittest tests.test_queue -v`
Erwartet: `ModuleNotFoundError: No module named 'slotmon.queue'`.

## Schritt 2: Die minimale Implementierung

```python
# slotmon/queue.py
"""Die Warteschlange in der Reihenfolge, in der der Server sie abarbeitet.

Rein: keine Uhr, kein I/O, kein HTTP. Die Zeit kommt von aussen.

Reihenfolge
-----------
Der Server arbeitet FIFO. Der aelteste `request.received` ist der naechste.
`LogReader.state["open"]` speichert `rid -> at`, wobei `at` die Unix-Zeit aus
dem `timestamp`-Feld ist - **mit Mikrosekunden**. Gemessen ueber 214 Zeilen:
null Kollisionen. Der Tie-Break sortiert deshalb zusaetzlich nach `rid`, damit
die Reihenfolge nicht von der Dict-Implementierung abhaengt, falls doch einmal
zwei im gleichen Mikrosekunden eintreffen.

Keine Namen fuer Wartende
-------------------------
Die IP steht ausschliesslich in `request.finished`. Ein Wartender hat keine -
gemessen an 61 wartenden Requests ueber alle Felder von `request.received`:
null Treffer. Der Name wird nur gezeigt, wenn dieselbe `ip_port` aus einem
frueheren Abschluss bekannt ist (`known_ips`). Sonst `None`, was die Anzeige
als "unbekannt, steht erst nach dem Abschluss fest" rendert.
"""

from dataclasses import dataclass, asdict


@dataclass
class WaitingEntry:
    position: int
    rid: str
    waiting_since: float
    system: str | None

    def as_dict(self):
        return asdict(self)


def build(open_rids, waiting, legacy, resolve):
    """Die Warteschlange als Liste, Position 1 zuerst.

    `open_rids` sind die rids, die ein `request.received` hatten und noch kein
    `request.finished`. Das sind Halter **und** Wartende; die Reihenfolge deckt
    beides ab.

    `legacy > 0` heisst: die Warteschlangenlaenge passt nicht zu den Zeilen im
    Log, die Liste waere geraten. Dann wird **nichts** geliefert - eine leere
    Liste, nicht eine unvollstaendige.
    """
    if legacy:
        return []
    if waiting is None:
        return []
    ordnung = sorted(open_rids.items(), key=lambda paar: (paar[1], paar[0]))
    # Nur so viele, wie der Server auch bestaetigt. Die Liste ist sonst
    # laenger als die Queue, und Position 2 existiert dann nicht.
    ordnung = ordnung[:max(0, waiting)]
    eintraege = []
    for index, (rid, since) in enumerate(ordnung, 1):
        system = resolve(rid)
        eintraege.append(WaitingEntry(
            position=index, rid=rid, waiting_since=since,
            system=system if isinstance(system, str) else None))
    return [e.as_dict() for e in eintraege]
```

## Schritt 3: Der grüne Lauf

`python3.12 -B -m unittest tests.test_queue -v` → `OK`.

## Schritt 4: Die Fälle, die noch fehlen

Ergänze **je einen Test** (erst rot laufen lassen, dann implementieren):

| Test | Der Mutant, der ihn töten muss |
|---|---|
| `legacy > 0` liefert `[]` | Liste wird aus `open_rids` trotzdem gebaut |
| `waiting is None` liefert `[]` | Liste erscheint ohne Serververbindung |
| Liste ist höchstens `waiting` lang | Überzählige rids tauchen auf |
| `resolve` gibt `None` → `system is None` | „unbekannt" wird zu einem Namen |
| `resolve` gibt eine leere Zeichenkette | Leerer String gilt als Name |
| Ein bekannter Name erscheint | Namens-Rückfall fehlt |
| Leere `open_rids` → `[]` | Exception bei leerer Eingabe |
| `waiting` kleiner als Zahl der offenen rids | `max(0, ...)` fällt weg |

## Schritt 5: An den Collector

`collector.py`, in `tick()` **nach** `open_rids` gebaut und **nach** `evaluate()`,
weil `legacy` aus `evaluate()` kommt:

```python
self._state["queue"]["waiting_list"] = queue.build(
    open_rids, waiting, slot_state.legacy, self._queue_name)
```

`_empty_state()` bekommt `"waiting_list": []`.

`_queue_name` ist eine kleine Methode: sie schlägt `ip_port` in
`self._known_ips` nach (aus bereits abgeschlossenen Einträgen) und ruft dann
`resolve(self.names, host_of(...))`. **Wichtig:** sie darf die Access-Zeile des
Lesers nicht verbrauchen — die gehört dem Halter.

## Schritt 6: Tests gegen den echten Collector

In `tests/test_collector.py`, zwei Tests:

- `state["queue"]["waiting_list"]` ist nach einem `tick()` mit drei offenen rids
  **drei** Einträge lang, Positionen 1-3, in der Reihenfolge der `received`-Zeiten.
- Bei `legacy > 0` ist die Liste **leer**, auch wenn offene rids da sind.

## Schritt 7: Commit

```bash
git add slotmon/queue.py slotmon/collector.py tests/test_queue.py tests/test_collector.py
git commit -m "feat: Warteschlange in der Reihenfolge, aus dem Log gelesen"
```

---

# Task 2: Das Abbrechen

**Files:**
- Create: `slotmon/abort.py`
- Create: `tests/test_abort.py`
- Modify: `slotmon/server.py`
- Modify: `tests/test_server.py`
- Modify: `tests/test_no_socket_source.py`

**Interfaces:**
- Consumes: `cfg`, `state` (ein `dict` aus `collector.state()`),
  `names`, `known_ips`
- Produces:
  - `slotmon.abort.abort_holder(cfg, state) -> dict`
  - `slotmon.abort.abort_waiting(cfg, state, position) -> dict`
  Beide: `{"aborted": bool, "rid": str | None, "reason": str}`

**Der einzige schreibende Aufruf im ganzen Monitor.**

## Schritt 1: Der rote Test — der gefährlichste zuerst

```python
# tests/test_abort.py
"""Der eine schreibende Zugriff des Monitors.

Jeder Test hier prueft eine Sache, die **nicht** passieren darf. Ein
"funktioniert"-Test waere wertlos - der Abbruch funktioniert oder er
funktioniert nicht, und interessant ist ausschliesslich, wo er zerstoert.
"""

import json
import unittest
from unittest import mock

from slotmon import abort


class AbortCase(unittest.TestCase):
    def setUp(self):
        self.cfg = mock.Mock()
        self.cfg.api = "http://127.0.0.1:30000"
        self.cfg.auth = "Bearer marec"
        self.busy = {
            "slot": {"state": "busy", "rid": "aaa111", "system": None,
                     "ip_port": None, "legacy": 0, "reason": None,
                     "open_count": 1, "complete": True,
                     "duration_s": 12.0, "started_at": 100.0},
            "queue": {"waiting": 2, "running": 1,
                      "waiting_list": [
                          {"position": 1, "rid": "bbb222", "waiting_since": 90.0,
                           "system": None},
                          {"position": 2, "rid": "ccc333", "waiting_since": 80.0,
                           "system": "Laptop"}]},
        }

    def post(self, body):
        """Faengt den POST ab und gibt den Body zur Pruefung zurueck."""
        with mock.patch.object(abort.httpx, "post") as post:
            post.return_value = mock.Mock(status_code=200)
            return body, post
```

Danach die Tests. **Reihenfolge ist hier nicht beliebig** — der wichtigste zuerst:

```python
class TestTheRequestItself(AbortCase):
    """Der erzeugte Request, nicht die Absicht.

    `io_struct.py:1541` macht aus `rid=None` die leere Zeichenkette, und
    `scheduler.py:2814` vergleicht mit `req.rid.startswith(recv_req.rid)`.
    `"x".startswith("")` ist **wahr** - eine leere rid wuerde die ganze
    Warteschlange loeschen, ohne dass `abort_all` gesetzt waere. Gemessen am
    echten Server: der `rid_to_state`-Guard im TokenizerManager haelt das auf,
    aber wir verlassen uns nicht auf eine fremde Verteidigung.
    """

    def test_never_sends_an_empty_rid(self):
        ergebnis, post = self.post(abort.abort_holder(self.cfg, self.busy))
        body = post.call_args.kwargs["json"]
        self.assertNotEqual(body["rid"], "")
        self.assertEqual(len(body["rid"]), 8, "aaa111")

    def test_never_sets_abort_all(self):
        _, post = self.post(abort.abort_holder(self.cfg, self.busy))
        body = post.call_args.kwargs["json"]
        self.assertNotIn("abort_all", body)
        self.assertIs(body.get("abort_all", False), False)

    def test_sends_exactly_one_rid(self):
        _, post = self.post(abort.abort_holder(self.cfg, self.busy))
        body = post.call_args.kwargs["json"]
        self.assertIsInstance(body["rid"], str)
        self.assertEqual(body["rid"], "aaa111")

    def test_never_posts_a_list(self):
        """`abort_all` ist die Sammelvariante. Es darf keine geben."""
        _, post = self.post(abort.abort_waiting(self.cfg, self.busy, 2))
        body = post.call_args.kwargs["json"]
        self.assertNotIsInstance(body, list)
```

```python
class TestHalterGuards(AbortCase):
    def test_no_holder_means_no_post(self):
        zustand = json.loads(json.dumps(self.busy))
        zustand["slot"]["state"] = "free"
        zustand["slot"]["rid"] = None
        ergebnis, post = self.post(abort.abort_holder(self.cfg, zustand))
        post.assert_not_called()
        self.assertFalse(ergebnis["aborted"])
        self.assertIn("Kein Auftrag", ergebnis["reason"])

    def test_legacy_blocks_the_holder(self):
        zustand = json.loads(json.dumps(self.busy))
        zustand["slot"]["legacy"] = 3
        ergebnis, post = self.post(abort.abort_holder(self.cfg, zustand))
        post.assert_not_called()
        self.assertIn("Altbestand", ergebnis["reason"])

    def test_a_reason_blocks_the_holder(self):
        """`reason` gesetzt heisst: der Monitor bezweifelt seinen Halter.

        Toter Code im Normalfall (`reason` wird nur bei `unknown` gesetzt),
        aber die Stelle faengt den Widerspruch, falls `state` spaeter
        erweitert wird.
        """
        zustand = json.loads(json.dumps(self.busy))
        zustand["slot"]["reason"] = "Geister-rid"
        ergebnis, post = self.post(abort.abort_holder(self.cfg, zustand))
        post.assert_not_called()
        self.assertIn("Geister", ergebnis["reason"])
```

```python
class TestWaitingGuards(AbortCase):
    def test_position_one_maps_to_the_first_entry(self):
        _, post = self.post(abort.abort_waiting(self.cfg, self.busy, 1))
        self.assertEqual(post.call_args.kwargs["json"]["rid"], "bbb222")

    def test_position_two_maps_to_the_second_entry(self):
        _, post = self.post(abort.abort_waiting(self.cfg, self.busy, 2))
        self.assertEqual(post.call_args.kwargs["json"]["rid"], "ccc333")

    def test_position_beyond_the_list_posts_nothing(self):
        ergebnis, post = self.post(abort.abort_waiting(self.cfg, self.busy, 3))
        post.assert_not_called()
        self.assertIn("gibt es nicht", ergebnis["reason"])

    def test_position_zero_and_negative_post_nothing(self):
        for position in (0, -1, -99):
            ergebnis, post = self.post(
                abort.abort_waiting(self.cfg, self.busy, position))
            post.assert_not_called()
            self.assertFalse(ergebnis["aborted"])

    def test_legacy_blocks_the_whole_list(self):
        zustand = json.loads(json.dumps(self.busy))
        zustand["slot"]["legacy"] = 2
        ergebnis, post = self.post(abort.abort_waiting(self.cfg, zustand, 1))
        post.assert_not_called()
        self.assertIn("Altbestand", ergebnis["reason"])

    def test_position_never_reaches_the_server_verbatim(self):
        """Der Nutzer tippt eine Zahl; die rid loest der Monitor auf.

        Damit kann keine rid verwechselt werden - die Zahl, die er sieht,
        ist genau die, die gemeint ist.
        """
        _, post = self.post(abort.abort_waiting(self.cfg, self.busy, 2))
        self.assertNotIn("position", post.call_args.kwargs["json"])
```

```python
class TestServerBehaviour(AbortCase):
    def test_server_unreachable_does_not_raise(self):
        import httpx
        with mock.patch.object(abort.httpx, "post",
                               side_effect=httpx.ConnectError("down")):
            ergebnis = abort.abort_holder(self.cfg, self.busy)
        self.assertFalse(ergebnis["aborted"])
        self.assertIn("nicht erreichbar", ergebnis["reason"])

    def test_http_500_is_reported_not_raised(self):
        with mock.patch.object(abort.httpx, "post") as post:
            post.return_value = mock.Mock(status_code=500)
            ergebnis = abort.abort_holder(self.cfg, self.busy)
        self.assertFalse(ergebnis["aborted"])
        self.assertIn("500", ergebnis["reason"])

    def test_a_success_is_only_claimed_when_the_server_agrees(self):
        with mock.patch.object(abort.httpx, "post") as post:
            post.return_value = mock.Mock(status_code=200)
            ergebnis = abort.abort_holder(self.cfg, self.busy)
        self.assertTrue(ergebnis["aborted"])
        self.assertEqual(ergebnis["rid"], "aaa111")
```

Führe aus: `python3.12 -B -m unittest tests.test_abort -v`
Erwartet: `ModuleNotFoundError: No module named 'slotmon.abort'`.

## Schritt 2: Die Implementierung

```python
# slotmon/abort.py
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
(`rid_to_state`-Guard im TokenizerManager, `tokenizer_manager.py:1395`), die
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
```

## Schritt 3: Der grüne Lauf

`python3.12 -B -m unittest tests.test_abort -v` → `OK`.

## Schritt 4: Die Endpunkte

`server.py`, **zwei** Routen. Beide nehmen so wenig wie moeglich:

```python
    @app.post("/api/abort--holder")
    def abort_holder():
        """Bricht den laufenden Auftrag ab. Ohne Parameter - mit Absicht."""
        return abort.abort_holder(cfg, collector.state())

    @app.post("/api/abort--waiting")
    async def abort_waiting(payload: dict):
        return abort.abort_waiting(cfg, collector.state(),
                                   (payload or {}).get("position"))
```

`cfg` muss in `create_app` verfügbar sein — entweder über einen Parameter
`create_app(collector, cfg=None)` mit `cfg = cfg or collector.cfg`, oder direkt
`collector.cfg`. **`collector.cfg` ist die Wahrheit**: eine Abweichung zwischen
beiden wäre genau die Art Doppelwahrheit, die in Task 8 schon einmal Schaden
angerichtet hat. Nimm `collector.cfg`.

**Wichtig:** Die Routen lesen `collector.state()` — den Zustand des **letzten
Ticks**, nicht einen frischen. Das ist Absicht: der Zustand, den der Nutzer
gesehen hat, ist der, auf den er klickt.

## Schritt 5: Tests für die Endpunkte

In `tests/test_server.py`:

| Test | Der Mutant |
|---|---|
| `POST /api/abort--holder` → 200, `aborted` bei busy | Route fehlt |
| Doppelstart auf `/api/abort--waiting` ohne Body → 200 mit Grund | 422 statt 200 |
| `{"position": 1}` → `rid` aus der Liste | Position wird ignoriert |
| `{"position": "x"}` → Ablehnung | Typprüfung fehlt |
| Der Endpunkt **liest** den Zustand und erfindet nichts | Er ruft den Server selbst |
| Bei totem Server 200 mit `aborted: false` | 500 |

**Und der Rahmen-Test**, in `tests/test_no_socket_source.py`:

```python
class TestOnlyOneFileWrites(unittest.TestCase):
    """Genau eine Datei schreibt je zum Server.

    Bisher pruefte der Test, dass niemand `/abort_request` aufruft. Jetzt
    gilt das Gegenteil: `abort.py` darf es, und sonst niemand. Der Test wird
    strenger, nicht weicher - ein `POST` in einem anderen Modul ist jetzt ein
    Fehler, wo es vorher nur unerwuenscht war.
    """

    POST_ZEUGEN = ("httpx.post", "requests.post", "urlopen", ".post(")

    def test_exactly_one_module_posts(self):
        schreibend = []
        for pfad in sorted(glob.glob(os.path.join(SLOTMON, "*.py"))):
            with open(pfad, encoding="utf-8") as handle:
                quelle = handle.read()
            if any(zeuge in quelle for zeuge in self.POST_ZEUGEN):
                schreibend.append(os.path.basename(pfad))
        self.assertEqual(schreibend, ["abort.py"],
                         f"mehr als eine Datei schreibt zum Server: {schreibend}")
```

## Schritt 6: `finish_reason` auswerten

`collector.py`, in `_flush_finished`, **vor** dem `_append`:

```python
aborted = record.get("finish_reason") == "abort"
```

`record["aborted"] = aborted` und in der Mittelung **nicht** mitzählen. Der Weg
durch den Code: `_flush_finished` schreibt in `history.jsonl`; die Mittelung sitzt
im **Dashboard** (`averageWait`/`averageDuration`) und wird über einen Filter
`if entry.get("aborted"): continue` ausgeschlossen.

**Beide Stellen brauchen einen Test.** Sonst vergisst das Dashboard die Trennung
beim nächsten Umbau — und das ist genau die Art Regression, die hier schon
vorgekommen ist.

## Schritt 7: Die Nachweise

```bash
find slotmon tests -name __pycache__ -type d -exec rm -rf {} +
python3.12 -B -m unittest discover -s tests -q      # 408 + neu, alle gruen
node --check auf dem <script>-Block von dashboard.html
ASCII-Zaehlung ueber alle geaenderten Dateien: 0 Zeichen > 127
```

Der Mutantenraster aus `/tmp/opencode/mutate_server.py` wird um **mindestens vier**
Mutanten erweitert, je einen für: leere rid, `abort_all: true`, Position ignoriert,
`abort` in der Dauer-Mittelung.

## Schritt 8: Commit(s)

```bash
git add slotmon/abort.py slotmon/server.py tests/test_abort.py tests/test_server.py tests/test_no_socket_source.py
git commit -m "feat: einzelne Auftraege abbrechen, mit acht Sicherheitspruefungen"

git add slotmon/collector.py slotmon/dashboard.html tests/test_collector.py
git commit -m "feat: abgebrochene Auftraege als eigenes Ereignis in der Historie"
```

---

# Abnahme

Nach Task 1 und Task 2 zusammen, **am laufenden System**:

1. Drei bis vier Requests parallel abschicken; das Dashboard listet die
   Wartenden in der Reihenfolge, Position 1 ist die älteste.
2. Die Liste zeigt „unbekannt †"; nach einem abgeschlossenen Auftrag von
   `127.0.0.1` steht bei einer späteren Position „Lokal (opencode & Co.)".
3. `Position 1 abbrechen` → Bestätigungsfenster mit der **vollständigen** rid →
   `num_waiting_reqs` sinkt um genau 1, die übrigen rids sind unverändert.
4. **Der laufende Auftrag läuft weiter** — das ist der Beweis, dass kein
   `abort_all` gesendet wurde.
5. Erneuter Klick auf dieselbe Position → „gibt es nicht mehr", die Zahl der
   Wartenden bleibt unverändert.
6. Der abgebrochene Auftrag erscheint im Verlauf als „abgebrochen" und **nicht**
   in der mittleren Slot-Dauer.
7. `sglang` läuft unverändert, `curl /get_load` antwortet, der Halter war nie
   betroffen.
8. Die bestehenden 408 Tests bleiben grün.