"""Tests fuer den Dashboard-Server: Routen, Zustand, HTML.

Das Dashboard ist eine HTML-Datei mit eingebettetem JavaScript. Fuer die
Verdrahtung von `slot.reason` bis ins Dokument wird der `<script>`-Block
herausgezogen und unter `node` mit einem DOM-Stub ausgefuehrt - der Test
prueft damit, was der Nutzer sieht, nicht die Buchstaben im Quelltext.
"""

import asyncio
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from slotmon.collector import Collector
from slotmon.config import Config, load_config
from slotmon.server import create_app
from slotmon.sources import Sample

DASHBOARD = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         os.pardir, "slotmon", "dashboard.html")


def read_dashboard():
    with open(DASHBOARD, encoding="utf-8") as handle:
        return handle.read()


class CollectorCase(unittest.TestCase):
    """Collector auf Temp-Dateien und ohne Quellen, nie gegen die echte Maschine.

    Zwei Gruende, beide aus `tests/test_collector.py` uebernommen:

    1. `Collector.tick()` rotiert das Server-Log und schreibt `history.jsonl`.
       Mit der Default-Konfiguration fasste ein Testlauf genau die Dateien an,
       die das Produkt beaufsichtigt - und `LogRotator` kuerzt das Server-Log
       ab 1 GB auf 0. Ein Test darf das nicht ausloesen koennen.
    2. Auf der Maschine, die diese Tests ausfuehrt, laeuft der sglang-Server
       unter `API` und ein echtes `nvidia-smi`. Ohne die Stub-Quellen liefe
       `test_missing_values_stay_null_not_zero` gegen die Live-Maschine und
       saehe `waiting=0` und `gpu.util=0` - nicht `None`. Genau diese
       Verwechslung ("0 heisst gemessen") soll der Test verhindern, also
       braucht er eine nachweislich tote Quelle.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="slotmon-server-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        # Der Basename muss auf `escha_server*.log` passen, sonst verweigert
        # LogRotator den Pfad.
        self.log = os.path.join(self.dir, "escha_server_test.log")
        with open(self.log, "w", encoding="utf-8"):
            pass
        self.monitor_log = os.path.join(self.dir, "slotmon.log")
        self.history = os.path.join(self.dir, "history.jsonl")
        self.cfg = load_config({
            "SERVER_LOG": self.log,
            "MONITOR_LOG": self.monitor_log,
            "HISTORY": self.history,
            # Port 1 nimmt nichts: `httpx` bekommt sofort `ConnectError`.
            "API": "http://127.0.0.1:1",
        })
        self.collector = Collector(self.cfg, {})
        self.addCleanup(self.collector.close)
        dead = lambda **kw: Sample(None, None, "Quelle im Test nicht erreichbar")
        for name in ("read_load", "read_metrics", "read_gpu", "read_server_info"):
            setattr(self.collector.sources, name, dead)


class TestEndpoints(CollectorCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.collector))

    def test_state_returns_the_collector_state(self):
        self.collector.tick()
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("slot", payload)
        self.assertIn("queue", payload)

    def test_missing_values_stay_null_not_zero(self):
        """Ohne Serverantwort bleibt alles null, nirgends steht eine 0."""
        self.collector.tick()
        payload = self.client.get("/api/state").json()
        self.assertIsNone(payload["queue"]["waiting"])
        self.assertIsNone(payload["queue"]["running"])
        self.assertIsNone(payload["speed"]["tok_per_s"])
        self.assertIsNone(payload["speed"]["output_pct"])
        self.assertIsNone(payload["gpu"]["util"])
        self.assertIsNone(payload["slot"]["rid"])

    def test_root_serves_html(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("slotmon", response.text.lower())

    def test_stream_route_is_registered(self):
        """Der SSE-Pfad wird am Router geprueft, nicht ueber einen offenen Stream.

        TestClient.stream auf einen endlosen Generator blockiert beim Verlassen des
        Kontextmanagers. Die media_type-Pruefung passiert deshalb live in Step 5,
        wo der Server als curl erreichbar ist.
        """
        routes = [r for r in create_app(self.collector).routes
                  if getattr(r, "path", "") == "/api/stream"]
        self.assertEqual(len(routes), 1)

    def test_health_endpoint(self):
        self.assertEqual(self.client.get("/health").json(), {"ok": True})


# ---------------------------------------------------------------------------
# F12 - der Tick darf nicht am SSE-Client haengen
# ---------------------------------------------------------------------------

class StubCollector:
    """Attrappe fuer `Collector`: zaehlt die Ticks, liefert einen festen Zustand.

    Fuer `create_app` ist nur `cfg.poll_interval`, `tick()` und `state()`
    relevant. Ein echter Collector wuerde hier Dateien anfassen und HTTP fragen
    - beides hat dieser Test nicht zu pruefen, und `CollectorCase` ist fuer die
    Faelle da, in denen es das doch ist.
    """

    def __init__(self, poll_interval=0.05, tick_error=None):
        self.cfg = Config(poll_interval=poll_interval)
        self.ticks = 0
        self.tick_error = tick_error

    def tick(self):
        self.ticks += 1
        if self.tick_error is not None:
            raise self.tick_error
        return self.state()

    def state(self):
        return {"at": 1791795000.0, "slot": {"state": "unknown"}, "history": []}


#: Scope fuer einen GET auf `/api/stream`. `spec_version` 2.4 ist wichtig:
#: darunter legt Starlette ein Task-Group um `stream_response`, und das
#: Abbrechen des Lese-Tasks wird dann unzuverlaessig.
STREAM_SCOPE = {
    "type": "http",
    "asgi": {"version": "3.0", "spec_version": "2.4"},
    "http_version": "1.1",
    "method": "GET",
    "path": "/api/stream",
    "raw_path": b"/api/stream",
    "query_string": b"",
    "headers": [],
    "client": ("127.0.0.1", 12345),
    "server": ("127.0.0.1", 80),
    "scheme": "http",
    "root_path": "",
}


async def read_stream(app, count, timeout=15.0):
    """`count` SSE-Rahmen aus `/api/stream` lesen, dann die Verbindung abbrechen.

    `TestClient` kann das nicht: `handle_request` ruft `portal.call(self.app,
    ...)`, und das laeuft die App **bis zum Ende** - bei einem endlosen
    Generator heisst das: blockieren. Der Test spricht deshalb direkt die
    ASGI-Schnittstelle an. Zwei `read_stream` im selben `asyncio.gather` sind
    wirklich gleichzeitige Leser, nicht zwei nacheinander.
    """
    chunks = []
    enough = asyncio.Event()

    async def receive():
        # Bei spec_version 2.4 fragt `StreamingResponse` gar nicht nach `receive`.
        # Das hier haelt die Schnittstelle vollstaendig, falls sich das aendert.
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                chunks.append(body)
                if len(chunks) >= count:
                    enough.set()

    task = asyncio.create_task(app(dict(STREAM_SCOPE), receive, send))
    try:
        await asyncio.wait_for(enough.wait(), timeout)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return chunks


def wait_until(predicate, timeout=10.0):
    """Warten, bis `predicate()` wahr ist. `True` bei Erfolg, `False` sonst."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class TestBackgroundTicker(unittest.TestCase):
    """Ohne Browser laeuft der Poll weiter - er haengt am Server, nicht am Client.

    Solange `tick()` im SSE-Generator stand, gab es ohne geoeffneten Browser
    keinen Poll, keine Historie und keine Log-Rotation. Die Schutzschicht gegen
    unbegrenztes Logwachstum (`truncate -s 0` ab 1 GB, Spec Z. 321-341) ruhte
    damit genau dann, wenn sie gebraucht wird.
    """

    def test_ticker_runs_without_any_sse_client(self):
        """Nur ein `/health`-Aufruf. Der Stream wird nie geoeffnet."""
        collector = StubCollector(poll_interval=0.02)
        with TestClient(create_app(collector)) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertTrue(wait_until(lambda: collector.ticks >= 3),
                            f"nur {collector.ticks} Tick(s) ohne SSE-Client")

    def test_one_ticker_per_lifespan_and_a_clean_shutdown(self):
        """Genau ein Ticker je Lifespan, und er ist danach beendet.

        `create_app` darf den Ticker nicht pro Anfrage starten - sonst haette
        jeder `/api/state`-Aufruf einen zweiten. Er darf ihn aber auch nicht
        nur "anlegen und vergessen": ohne `cancel()` laeuft er beim Herunterfahren
        in die Warnung "Task was destroyed but it is pending".
        """
        collector = StubCollector(poll_interval=0.02)
        app = create_app(collector)
        with TestClient(app) as client:
            ticker = app.state.ticker
            for _ in range(3):
                client.get("/health")
            self.assertIs(app.state.ticker, ticker)
            self.assertFalse(ticker.done())
        self.assertTrue(ticker.done())

        # Ein neuer Lifespan-Durchlauf bekommt einen neuen Task - der alte
        # wird nicht wiederbelebt, und der zweite haengt nicht am ersten.
        with TestClient(app):
            self.assertIsNot(app.state.ticker, ticker)
            self.assertFalse(app.state.ticker.done())

    def test_a_failing_tick_does_not_stop_the_ticker(self):
        """Ein `tick()`, das wirft, beendet den Ticker nicht.

        Der Fehler wird protokolliert und der naechste Poll laeuft normal
        weiter: "Ein Fehler im Monitor darf nie als Signal erscheinen".
        """
        collector = StubCollector(poll_interval=0.02,
                                  tick_error=RuntimeError("Poll kaputt"))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with TestClient(create_app(collector)) as client:
                self.assertEqual(client.get("/health").status_code, 200)
                ticked = wait_until(lambda: collector.ticks >= 3)
        self.assertTrue(ticked, f"nur {collector.ticks} Tick(s) nach dem Fehler")
        self.assertIn("tick-error", stderr.getvalue())

    def test_stream_reader_alone_never_ticks(self):
        """Der SSE-Generator pollt nicht - auch nicht der erste Leser.

        Deterministisch statt zeitbasiert: es laeuft gar kein Ticker, weil der
        Lifespan nie betreten wird. Haette `events()` noch `collector.tick()`
        aufgerufen, waere `ticks` nach drei Frames groesser als null.
        """
        collector = StubCollector()
        app = create_app(collector)
        chunks = asyncio.run(read_stream(app, 3))
        self.assertEqual(len(chunks), 3)
        self.assertEqual(collector.ticks, 0)

    def test_three_readers_share_one_ticker_rate(self):
        """Drei gleichzeitige Leser, ein Takt.

        Gelesen wird aus jeder Verbindung `EVENTS` Rahmen; das dauert etwa
        `EVENTS * poll_interval`, in denen der Ticker rund `EVENTS` Mal laeuft.
        Haette jeder Leser seinen eigenen Takt, waeren es `3 * EVENTS` - die
        Assertion `ticks < 2 * EVENTS` faellt dann, sie haelt aber mit einer
        Spanne von +-100 % Reserve gegen einen zu langsamen Rechner.
        """
        events = 10
        collector = StubCollector(poll_interval=0.05)
        app = create_app(collector)

        async def scenario():
            async with app.router.lifespan_context(app):
                await asyncio.gather(*[read_stream(app, events) for _ in range(3)],
                                     return_exceptions=True)

        asyncio.run(scenario())
        self.assertLess(collector.ticks, 2 * events,
                        f"{collector.ticks} Ticks bei {3 * events} Frames "
                        f"- das ist ein Takt je Leser")


class TestHtml(unittest.TestCase):
    def test_dashboard_file_exists_and_has_both_tabs(self):
        html = read_dashboard()
        self.assertIn('data-tab="now"', html)
        self.assertIn('data-tab="history"', html)

    def test_dashboard_has_the_altbestand_banner(self):
        self.assertIn("altbestand", read_dashboard().lower())

    def test_dashboard_has_no_prompt_field(self):
        html = read_dashboard()
        self.assertNotIn("prompt_text", html)
        self.assertNotIn("promptText", html)


#: `<script>`-Block aus `dashboard.html`. Es gibt genau einen; der Test schlaegt
#: sich, falls es je mehr werden.
SCRIPT_BLOCK = re.compile(r"<script>(?P<code>.*?)</script>", re.DOTALL)

#: DOM-Stub fuer die Ausfuehrung von `render()`. Nur die Glieder, die
#: `render()` tatsaechlich anfasst: `id`, `textContent`, `innerHTML`,
#: `className`, `classList`, `style`. Alles andere existiert nicht - ein
#: Element, das `render()` nicht kennt, ist dann ein harter Fehler und kein
#: stilles `undefined`.
STUB = """
const __els = {};
function __el(id) {
  if (!(id in __els)) {
    const set = new Set();
    __els[id] = {
      _id: id, _classes: set,
      textContent: "", innerHTML: "", className: "", value: "",
      dataset: {}, style: {},
      classList: {
        add: (c) => set.add(c),
        remove: (c) => set.delete(c),
        contains: (c) => set.has(c),
      },
    };
  }
  return __els[id];
}
// Nur die Elemente, die wirklich in der HTML-Datei stehen. `getElementById`
// gibt fuer alles andere `null`, wie im Browser: ein umbenanntes `id` laesst
// `render()` dann mit `TypeError` abbrechen, statt still zu nichts zu tun.
const __ids = JSON.parse(process.argv[3]);
for (const id of __ids) __el(id);
const document = {
  getElementById: (id) => (id in __els ? __els[id] : null),
  querySelectorAll: () => [],
  querySelector: () => null,
};
const sessionStorage = { getItem: () => null, setItem: () => {} };
// Kette aus `.then().then()`, die nie ausgefuehrt wird: der Test ruft
// `render()` selbst. Ein Stub, der zurueckbricht, wuerde den Test an der
// Harness-Schnur statt am Verhalten scheitern lassen.
const fetch = () => ({ then: () => ({ then: () => ({}) }) });
const EventSource = class { constructor(url) { this.url = url; } };
function __dump() {
  const out = {};
  for (const id of Object.keys(__els)) {
    const el = __els[id];
    out[id] = {
      text: el.textContent,
      html: el.innerHTML,
      cls: el.className,
      classes: Array.from(el._classes),
      width: el.style.width === undefined ? null : el.style.width,
    };
  }
  return out;
}
"""

#: Nach `render()`: den DOM-Zustand als JSON ausgeben. Genau das, was der
#: Betrachter im Browser sieht - nicht die Quelltext-Zeile, die es erzeugt.
RUNNER = """
const __state = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
render(__state);
process.stdout.write(JSON.stringify(__dump()));
"""

#: `node` oder `nodejs`. Ohne einen davon laeuft der `render()`-Test nicht -
#: dann wird er uebersprungen, aber nicht als bestanden gezaehlt.
NODE = shutil.which("node") or shutil.which("nodejs")


def render(state):
    """`render()` aus `dashboard.html` mit einem DOM-Stub ausfuehren.

    Der HTML-Text wird nicht geparst und nicht mit `assertIn` abgetastet:
    `render()` laeuft, bekommt `state` und liefert zurueck, was danach in den
    Elementen steht. Geprueft wird damit das Verhalten und nicht eine
    Formulierung im Quelltext - ein umbenanntes `id` faellt sofort auf, ein
    umgestellter `if` nicht.
    """
    html = read_dashboard()
    match = SCRIPT_BLOCK.search(html)
    if match is None:
        raise AssertionError("dashboard.html hat keinen <script>-Block")
    ids = sorted(set(re.findall(r'id="([^"]+)"', html)))
    program = STUB + match.group("code") + RUNNER
    with tempfile.TemporaryDirectory(prefix="slotmon-render-") as work:
        script = os.path.join(work, "render.js")
        payload = os.path.join(work, "state.json")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(program)
        with open(payload, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        done = subprocess.run([NODE, script, payload, json.dumps(ids)],
                              capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        raise AssertionError(f"render() ist abgebrochen:\n{done.stderr}")
    return json.loads(done.stdout)


def entry(**overrides):
    """Ein abgeschlossener Eintrag aus `history.jsonl`, alle Felder bekannt.

    Wie `sample_state`: der Test soll an dem einen Feld liegen, an dem er
    gerade etwas prueft. `ended_at` ist eine Unix-Zeit, weil der Zeitstrahl
    daraus einen Zeitstempel macht.
    """
    record = {
        "rid": "rid-x", "ip_port": "192.168.9.2:44444", "system": "Laptop",
        "started_at": 1791794900.0, "ended_at": 1791795000.0, "duration_s": 100.0,
        "prompt_tokens": 1000, "completion_tokens": 500, "cached_tokens": 50,
        "finish_reason": "stop", "status": 200, "received_time": 4711.0,
    }
    record.update(overrides)
    return record


def sample_state(**overrides):
    """Ein vollstaendiger Zustand wie `Collector.state()`, alle Werte bekannt.

    Jeder Abschnitt laesst sich einzeln ueberschreiben - der Test soll an dem
    einen Feld liegen, nicht an vier anderen.
    """
    state = {
        "at": 1791795000.0,
        "server": {"connected": True, "load_error": None, "age_s": 0.1},
        "slot": {
            "state": "unknown", "rid": None, "system": None, "ip_port": None,
            "started_at": None, "duration_s": None, "legacy": None,
            "open_count": 0, "complete": False, "reason": None,
        },
        "queue": {"waiting": 3, "running": 1},
        "speed": {"tok_per_s": 42.5, "ctx_tokens": 42000,
                  "max_req_input_len": 83994, "output_pct": 50.0},
        "gpu": {"util": 97, "used_mb": 14592, "total_mb": 16303},
        "log": {"size_mb": 12.0, "warn": False, "truncated": False,
                "history_error": None},
        "history": [],
    }
    for section, value in overrides.items():
        if isinstance(value, dict) and isinstance(state.get(section), dict):
            state[section] = {**state[section], **value}
        else:
            state[section] = value
    return state


@unittest.skipUnless(NODE, "kein node/nodejs im PATH - render() wird nicht ausgefuehrt")
class TestRenderedSlotNote(unittest.TestCase):
    """`slot-note` bekommt `slot.reason`, sonst den Altbestand-Satz, sonst nichts.

    `unknown` ohne sichtbaren Grund ist im Dashboard von einem echten Leerlauf
    nicht zu unterscheiden, und genau diese Verwechslung hat den Phantom-Halter
    aus Runde 2 glaubhaft gemacht. Deshalb hat `reason` Vorrang vor jedem
    generischen Satz.
    """

    REASON = ("num_reqs=0, aber 2 rid(s) im Log laenger als 2 s offen - "
              "der Server kennt sie nicht")

    def note(self, **overrides):
        return render(sample_state(**overrides))["slot-note"]["text"]

    def test_reason_replaces_the_generic_altbestand_sentence(self):
        """`reason` ist der Text der Zeile - im `unknown`-Fall der einzige."""
        self.assertEqual(self.note(slot={"state": "unknown", "complete": False,
                                         "reason": self.REASON}),
                         self.REASON)

    def test_reason_wins_even_when_the_altbestand_sentence_would_apply(self):
        """Auch mit `complete == False` bleibt `reason` allein stehen."""
        note = self.note(slot={"state": "unknown", "complete": False, "legacy": 17,
                               "reason": self.REASON})
        self.assertEqual(note, self.REASON)
        self.assertNotIn("Altbestand", note)

    def test_without_reason_the_altbestand_sentence_stands(self):
        """Kein `reason` und `complete == False`: der alte Satz, sonst nichts."""
        note = self.note(slot={"state": "unknown", "complete": False,
                               "reason": None})
        self.assertNotEqual(note, "")
        self.assertIn("Halter", note)

    def test_empty_reason_is_treated_as_no_reason(self):
        """`reason: ""` ist kein Grund. Der Leerstring darf nicht gewinnen."""
        note = self.note(slot={"state": "unknown", "complete": False, "reason": ""})
        self.assertIn("Halter", note)

    def test_complete_slot_without_reason_leaves_the_note_empty(self):
        """`complete` und kein `reason`: nichts behaupten, was nicht belegt ist."""
        self.assertEqual(self.note(slot={"state": "free", "complete": True,
                                         "reason": None}), "")


#: Ein Zeitstrahl-Eintrag. Die Farbe steht im `style`, damit der Test sie am
#: fertigen Element sieht und nicht an einer Quelltext-Zeile.
TL_ENTRY = re.compile(
    r'<div class="tl-entry ([^"]*)" style="border-left:3px solid ([^"]*)">(.*?)</div>',
    re.DOTALL)


def timeline(dom):
    """Die Zeitstrahl-Eintraege als (klasse, farbe, html), in Reihenfolge."""
    return TL_ENTRY.findall(dom["timeline"]["html"])


def meta_of(entry_html):
    """Der graue Mittelteil eines Eintrags - die Zahlenaussage, exakt."""
    found = re.search(r'<span class="muted">(.*?)</span>', entry_html, re.DOTALL)
    if found is None:
        raise AssertionError(f"der Eintrag hat keinen <span class=muted>: {entry_html}")
    return found.group(1)


def entry_with(dom, needle):
    """Der eine Zeitstrahl-Eintrag, der `needle` enthaelt."""
    found = [entry for entry in timeline(dom) if needle in entry[2]]
    if len(found) != 1:
        raise AssertionError(
            f"{len(found)} Eintraege mit {needle!r}, erwartet genau einer: "
            f"{[entry[2] for entry in timeline(dom)]}")
    return found[0]


@unittest.skipUnless(NODE, "kein node/nodejs im PATH - render() wird nicht ausgefuehrt")
class TestTimeline(unittest.TestCase):
    """Der Zeitstrahl fuehrt den laufenden Slot mit - nicht nur die Historie.

    Spec Z. 297-304: "Zeitstrahl der Slot-Uebergaben: LAEUFT (gruen), FERTIG
    (grau) mit Dauer, ctx, tok/s und Tokenzahlen je Eintrag", und Z. 262-264:
    bei freiem Slot `BEREIT FUER NAECHSTEN` **ohne Namen**.

    Geprueft wird exakt, nicht mit `assertIn` auf eine Zahl: `"2" in "182"` ist
    erfuellt, und ein gruener Test, der eine Beimischung nicht sieht, ist
    schlimmer als keiner.
    """

    BUSY = {"state": "busy", "rid": "rid-busy", "system": "Laptop",
            "ip_port": "192.168.9.2:44444", "duration_s": 63.0, "complete": False}

    def test_busy_slot_gets_a_running_entry_in_green(self):
        dom = render(sample_state(slot=self.BUSY))
        cls, colour, html = entry_with(dom, "LAEUFT")
        self.assertIn("running", cls)
        # Die gruene Farbe ist die vorhandene `--ok`, nicht eine neue.
        self.assertEqual(colour, "var(--ok)")
        self.assertIn("Laptop", html)
        self.assertEqual(meta_of(html),
                         "LAEUFT - 1m 3s - ctx 42000 - 42.5 tok/s - 50.00 % Ausgabe")
        self.assertIn("rid rid-busy", html)

    def test_busy_slot_without_a_name_falls_back_to_the_ip(self):
        """Ohne Namen ist die IP die einzige belegte Zuordnung."""
        dom = render(sample_state(slot={**self.BUSY, "system": None}))
        _, _, html = entry_with(dom, "LAEUFT")
        self.assertIn("192.168.9.2:44444", html)

    def test_free_slot_shows_beret_ready_without_any_name(self):
        """`BEREIT FUER NAECHSTEN` - und sonst nichts, was einen Namen traegt.

        Der Zustand traegt hier absichtlich einen Namen **und** eine IP. Beide
        duerfen im Eintrag nicht auftauchen: die Wartenden lassen sich keinem
        System zuordnen, und jeder Name waere geraten.
        """
        dom = render(sample_state(slot={"state": "free", "complete": True,
                                        "system": "Phantom", "rid": "rid-frei",
                                        "ip_port": "192.168.9.2:44444",
                                        "duration_s": None}))
        entries = timeline(dom)
        self.assertEqual(len(entries), 1)
        html = entries[0][2]
        self.assertIn("BEREIT FUER NAECHSTEN", html)
        self.assertNotIn("Phantom", html)
        self.assertNotIn("192.168.9.2", html)
        self.assertNotIn("rid-frei", html)

    def test_unknown_slot_gets_no_running_entry(self):
        """Bei `unknown` ist der Halter nicht bestimmbar.

        Ein gruener Balken waere eine Behauptung. Stattdessen wird der Zustand
        benannt - mit `slot.reason`, wenn der Collector einen geliefert hat.
        """
        reason = "Halter nicht bestimmbar - kein Befund"
        dom = render(sample_state(slot={"state": "unknown", "complete": False,
                                        "reason": reason}))
        html = timeline(dom)[0][2]
        self.assertNotIn("LAEUFT", html)
        self.assertNotIn("BEREIT FUER NAECHSTEN", html)
        self.assertIn("UNBEKANNT", html)
        self.assertEqual(meta_of(html), reason)

    def test_finished_entry_carries_its_own_numbers(self):
        """Jeder Eintrag traegt **seine** Zahlen, exakt."""
        dom = render(sample_state(slot={"state": "free", "complete": True},
                                  history=[
                                      {"rid": "rid-alt", "system": "Alt",
                                       "ip_port": "192.168.9.1:1", "started_at": 1.0,
                                       "ended_at": 100.0, "duration_s": 7.0,
                                       "prompt_tokens": 111, "completion_tokens": 14,
                                       "cached_tokens": 2, "finish_reason": "stop",
                                       "status": 200, "received_time": 4711.0},
                                      {"rid": "rid-neu", "system": "Neu",
                                       "ip_port": "192.168.9.2:2", "started_at": 2.0,
                                       "ended_at": 102.0, "duration_s": 4.0,
                                       "prompt_tokens": 400, "completion_tokens": 20,
                                       "cached_tokens": 12, "finish_reason": "stop",
                                       "status": 200, "received_time": 4712.0},
                                  ]))
        entries = timeline(dom)
        # Neuester zuerst, und der laufende Zustand steht darueber.
        self.assertIn("BEREIT FUER NAECHSTEN", entries[0][2])
        self.assertEqual(entry_with(dom, "rid-neu")[1], "var(--idle)")
        self.assertEqual(meta_of(entry_with(dom, "rid-neu")[2]),
                         "FERTIG - 4s - ctx 400 - 5.0 tok/s - 20 tok - 12 cache")
        self.assertEqual(meta_of(entry_with(dom, "rid-alt")[2]),
                         "FERTIG - 7s - ctx 111 - 2.0 tok/s - 14 tok - 2 cache")

    def test_rate_is_the_mean_over_the_lifetime(self):
        """20 Ausgabetokens in 4 s sind 5.0 tok/s (Spec Z. 271-272)."""
        dom = render(sample_state(history=[entry(duration_s=4.0,
                                                completion_tokens=20)]))
        self.assertEqual(meta_of(entry_with(dom, "FERTIG")[2]),
                         "FERTIG - 4s - ctx 1000 - 5.0 tok/s - 20 tok - 50 cache")

    def test_zero_duration_gives_a_dash_not_nan(self):
        """`duration_s === 0`: kein Quotient. `0/0` waere `NaN`, `20/0` `Infinity`."""
        for duration_s, tokens in ((0.0, 20), (0.0, 0), (None, 20)):
            dom = render(sample_state(history=[entry(duration_s=duration_s,
                                                    completion_tokens=tokens)]))
            meta = meta_of(entry_with(dom, "FERTIG")[2])
            self.assertEqual(meta.count("- tok/s"), 1,
                             f"duration_s={duration_s}, tokens={tokens}: {meta}")
            self.assertNotIn("NaN", dom["timeline"]["html"])
            self.assertNotIn("Infinity", dom["timeline"]["html"])

    def test_history_entry_without_numbers_shows_dashes(self):
        dom = render(sample_state(history=[entry(duration_s=None,
                                                completion_tokens=None,
                                                prompt_tokens=None,
                                                cached_tokens=None)]))
        self.assertEqual(meta_of(entry_with(dom, "FERTIG")[2]),
                         "FERTIG - - - ctx - - - tok/s - - tok - - cache")


class TestOpenCount(unittest.TestCase):
    """`open_count` wird nicht angezeigt.

    Die Zahl offener rid(s) im Log bildet die Warteschlange nicht ab (gemessen:
    35 zu 1 Verbindungen bei 180 zu 1492 Wartenden). Die Spec hat die Kachel
    VERBINDUNGEN deshalb ausdruecklich gestrichen: "Eine Kachel VERBINDUNGEN
    gibt es nicht mehr - sie beruhte auf der widerlegten Socket-Annahme."

    Beischaetzt mit der Wartenden-Zahl waere es eine Zustaendigkeit, die nicht
    belegt ist - das ist der Fehler, den dieser Test verhindert.
    """

    def test_no_connections_tile_exists(self):
        """Es gibt nur die fuenf Kacheln der Spec, keine siebte."""
        html = read_dashboard()
        tiles = re.findall(r'<div class="label"[^>]*>([^<]*)</div>'
                          r'<div class="value" id="([^"]*)">', html)
        labels = dict((ident, label) for label, ident in tiles)
        self.assertNotIn("k-conn", labels)
        self.assertNotIn("k-conn", html)
        # Das KPI-Raster aus tab-now, wo die Kachel gestrichen wurde. `h-*`
        # gehoeren zu tab-history, `s-*` sind die vier Kennzahlen der
        # Slot-Karte. `k-vram` ist durch die Fehlerbehandlung der Spec
        # gedeckt ("GPU- und VRAM-Kachel zeigen -").
        self.assertEqual(sorted(i for i in labels if i.startswith("k-")),
                         ["k-gpu", "k-slot", "k-tps", "k-vram",
                          "k-wait", "k-wait-time"])
        # Die Beschriftung ist Teil der Aussage: WARTEND ist `num_waiting_reqs`,
        # nicht irgendeine Zahl. Ein umbenanntes Kachel-Label waere eine
        # Behauptung, die der Monitor nicht belegen kann.
        self.assertEqual(labels["k-wait"], "Wartend")

    @unittest.skipUnless(NODE, "kein node/nodejs im PATH - render() wird nicht ausgefuehrt")
    def test_open_count_never_reaches_the_waiting_number(self):
        """Die Wartenden-Zahl bleibt exakt die Wartenden-Zahl.

        Geprueft wird **exakt**, nicht mit `assertIn`: eine Beimischung ergaebe
        `2 + 180 = 182`, und ein blosses "2 steckt in 182" wuerde einen
        gruenen Test liefern, der die Beimischung nicht sieht.
        """
        dom = render(sample_state(slot={"open_count": 2},
                                  queue={"waiting": 180}))
        self.assertEqual(dom["k-wait"]["html"], "180")
        self.assertNotIn("k-conn", dom)


if __name__ == "__main__":
    unittest.main()