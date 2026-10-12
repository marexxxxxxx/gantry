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
      dataset: {}, style: {}, attrs: {},
      classList: {
        add: (c) => set.add(c),
        remove: (c) => set.delete(c),
        contains: (c) => set.has(c),
      },
    };
    // `title` und Freunde. Im Browser wird der Titel aus dem Attribut
    // gelesen; ein `title` nur im Quelltext waere fuer den Test unsichtbar.
    // Eigene Funktionen statt Pfeile: ein Pfeil in einem Objektliteral hat
    // kein eigenes `this`, `__els[id]` waere dort nicht erreichbar.
    __els[id].setAttribute = (k, v) => { __els[id].attrs[k] = v; };
    __els[id].getAttribute = (k) => (k in __els[id].attrs ? __els[id].attrs[k] : null);
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
// Timer werden nicht ausgefuehrt - `node` wuerde sonst ewig haengen. Die
// Rueckrufe werden aber **gesammelt**, damit der Test sie einmal aufrufen
// kann: die Altersanzeige soll ohne SSE-Tick weiterlaufen, und das ist nur
// pruefbar, wenn der Tick wirklich beim Warten ausgefuehrt wird.
const __timers = [];
const setInterval = (fn) => { __timers.push(fn); return __timers.length; };
function __runTimers() { for (const fn of __timers) fn(); }
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
      attrs: el.attrs,
    };
  }
  out["#timers"] = __timers.length;
  return out;
}
"""

#: Nach `render()`: den DOM-Zustand als JSON ausgeben. Genau das, was der
#: Betrachter im Browser sieht - nicht die Quelltext-Zeile, die es erzeugt.
#: Mehrere Zustaende hintereinander sind eine Liste: Puffer wie die Sparkline
#: brauchen mehrere `render()`-Aufrufe. Der Zwischenstand wird mit
#: ausgegeben, sonst koennte der Test nur den **letzten** Zustand pruefen -
#: und genau der ist bei einem Puffer der interessanteste.
RUNNER = """
const __states = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
const __list = Array.isArray(__states);
const __states_ = __list ? __states : [__states];
const __dumps = [];
for (const state of __states_) { render(state); __dumps.push(__dump()); }
// `process.argv[4]` schaltet die gesammelten Timer einmal aus. Ohne das
// waere die Altersanzeige nach einem Fehler eine eingefrorene Zahl.
if (process.argv[4] === "timers") __runTimers();
process.stdout.write(JSON.stringify(__list && process.argv[4] !== "timers"
  ? __dumps : __dump()));
"""

#: `node` oder `nodejs`. Ohne einen davon laeuft der `render()`-Test nicht -
#: dann wird er uebersprungen, aber nicht als bestanden gezaehlt.
NODE = shutil.which("node") or shutil.which("nodejs")


def render(state, more=(), run_timers=False):
    """`render()` aus `dashboard.html` mit einem DOM-Stub ausfuehren.

    Der HTML-Text wird nicht geparst und nicht mit `assertIn` abgetastet:
    `render()` laeuft, bekommt `state` und liefert zurueck, was danach in den
    Elementen steht. Geprueft wird damit das Verhalten und nicht eine
    Formulierung im Quelltext - ein umbenanntes `id` faellt sofort auf, ein
    umgestellter `if` nicht.

    `more` sind weitere Zustaende in Reihenfolge. Sie sind noetig, wo etwas
    **im Dokument** ueber die Aufrufe hinweg liegt: die Sparkline ist ein
    Puffer, und der gemessene Poll-Takt braucht zwei `data.at`. Der
    Rueckgabewert ist dann eine **Liste** mit einem Eintrag je `render()`,
    sonst ein einzelnes Element-Objekt.

    `run_timers` fuehrt die gesammelten `setInterval`-Rueckrufe einmal aus und
    gibt den Endzustand zurueck. Damit ist pruefbar, dass die
    Altersanzeige ohne einen SSE-Tick weiterlaeuft - eine eingefrorene Zahl
    waere nach einer Minute eine Luege.
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
            json.dump([state, *more] if more else state, handle)
        done = subprocess.run([NODE, script, payload, json.dumps(ids),
                               "timers" if run_timers else "no-timers"],
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


class TestNameHint(unittest.TestCase):
    """Wer seinen Namen erst durch den Abschluss erfuhr, sagt das.

    Spec Z. 303-304: "Eintraege, deren Systemname erst durch den Abschluss
    bekannt wurde, zeigen ihn mit dem Hinweis `Name nachtraeglich`".

    `collector.py` setzt `record["system"] = resolve(...)` **erst im Moment des
    Abschlusses** - waehrend des Laufs stand der Name nie fest. Der Hinweis
    gehoert also an **jeden** Eintrag mit Name oder IP, und an keinen ohne.
    """

    def hint_of(self, dom):
        found = [record for record in timeline(dom) if "FERTIG" in record[2]]
        if len(found) != 1:
            raise AssertionError(f"{len(found)} FERTIG-Eintraege, erwartet einer")
        return found[0][2]

    def test_named_entry_carries_the_hint(self):
        dom = render(sample_state(history=[entry(system="Laptop")]))
        html = self.hint_of(dom)
        self.assertIn("Laptop", html)
        self.assertIn("Name nachtraeglich", html)

    def test_ip_only_entry_carries_the_hint_too(self):
        """Nur die IP aufgeloest, kein Name: der Name war trotzdem erst spaeter da."""
        dom = render(sample_state(history=[entry(system=None)]))
        html = self.hint_of(dom)
        self.assertIn("192.168.9.2:44444", html)
        self.assertIn("Name nachtraeglich", html)

    def test_entry_without_ip_and_name_says_so_instead(self):
        """Ohne Name **und** ohne IP ist das ein anderer Fall.

        Die Abschlusszeile trug keine Client-Adresse. Das ist nicht "der Name
        kam spaeter", das ist "es gibt keine Zuordnung" - und `Name
        nachtraeglich` waere hier eine falsche Aussage.
        """
        dom = render(sample_state(history=[entry(system=None, ip_port=None)]))
        html = self.hint_of(dom)
        self.assertIn("keine IP im Log", html)
        self.assertNotIn("Name nachtraeglich", html)

    def test_the_running_entry_has_no_hint(self):
        """Der laufende Slot kennt seinen Namen noch nicht - der Hinweis
        gehoert zu abgeschlossenen Eintraegen, nicht zum laufenden."""
        dom = render(sample_state(slot={"state": "busy", "rid": "r", "system": None,
                                        "duration_s": 5.0}))
        self.assertNotIn("Name nachtraeglich", timeline(dom)[0][2])


def hidden(dom, ident):
    """`hidden` als Klasse gesetzt? Das Banner ist dann unsichtbar."""
    return "hidden" in dom[ident]["classes"]


#: Die Elemente des Reiters "Jetzt". Die Zeitstrahl-Eintraege stehen im
#: Reiter "Verlauf" und zaehlen hier **nicht** mit: dort ist der `slot.reason`
#: unsichtbar, und der `UNBEKANNT`-Eintrag ist die einzige Stelle, an der der
#: Grund im Dokument steht. Gezaehlt wird, was gleichzeitig sichtbar ist.
NOW_TAB = ("altbestand", "log-warn", "log-truncated", "log-error",
           "slot-note", "slot-system", "slot-ip", "slot-rid")


def now_tab(dom):
    """Alle Elemente des Reiters "Jetzt" als ein Text."""
    return " ".join(dom[ident]["text"] + dom[ident]["html"]
                    for ident in NOW_TAB if ident in dom)


@unittest.skipUnless(NODE, "kein node/nodejs im PATH - render() wird nicht ausgefuehrt")
class TestLogNotices(unittest.TestCase):
    """`data.log` wird gelesen. Vorher las niemand davon.

    Spec Z. 337: "**ab 900 MB** -> Warnung im Dashboard 'Log naehert sich 1 GB
    (912 MB)'". `size_mb`, `warn`, `truncated` und `history_error` waren
    tote Felder im Zustand.
    """

    #: Wortlaut aus `collector.py` (`BLIND_REASON`). Der Collector protokolliert
    #: und begruendet mit demselben Satz - steht er in `slot.reason`, darf das
    #: Dashboard ihn nicht ein zweites Mal behaupten.
    BLIND = ("log-truncated: die offenen rid(s) sind nicht mehr beobachtbar, "
             "bis wieder ein request.received im Log steht")

    def test_warn_shows_the_real_size(self):
        dom = render(sample_state(log={"warn": True, "size_mb": 912.0}))
        self.assertEqual(dom["log-warn"]["text"], "Log naehert sich 1 GB (912 MB)")
        self.assertFalse(hidden(dom, "log-warn"))

    def test_warn_without_a_measured_size_invents_no_number(self):
        """`warn` ist die belegte Aussage, `size_mb` nur die Beilage.

        `size_mb === null` heisst "nicht gemessen" (uebersprungener Takt oder
        nicht freigegebener Pfad), nicht "0 MB".
        """
        dom = render(sample_state(log={"warn": True, "size_mb": None}))
        self.assertEqual(dom["log-warn"]["text"], "Log naehert sich 1 GB")
        self.assertFalse(hidden(dom, "log-warn"))

    def test_without_warning_the_banner_stays_away(self):
        dom = render(sample_state(log={"warn": False, "size_mb": 12.0}))
        self.assertTrue(hidden(dom, "log-warn"))
        self.assertEqual(dom["log-warn"]["text"], "")

    def test_truncated_says_so_when_the_slot_reason_does_not(self):
        dom = render(sample_state(log={"truncated": True}, slot={"reason": None}))
        self.assertFalse(hidden(dom, "log-truncated"))
        self.assertIn("geleert", dom["log-truncated"]["text"])
        self.assertIn("nicht mehr beobachtbar", dom["log-truncated"]["text"])

    def test_truncated_is_not_repeated_against_the_slot_reason(self):
        """Dieselbe Aussage zweimal im selben Bild ist ein Fehler, kein Beleg.

        Gezaehlt wird nur der Reiter "Jetzt" - im Reiter "Verlauf" ist der
        `slot-reason` unsichtbar, und der `UNBEKANNT`-Eintrag dort ist die
        einzige Stelle, an der der Grund steht.
        """
        dom = render(sample_state(log={"truncated": True},
                                  slot={"state": "unknown", "reason": self.BLIND}))
        self.assertEqual(dom["slot-note"]["text"], self.BLIND)
        self.assertTrue(hidden(dom, "log-truncated"))
        self.assertEqual(dom["log-truncated"]["text"], "")
        self.assertEqual(now_tab(dom).count("nicht mehr beobachtbar"), 1,
                         f"{now_tab(dom).count('nicht mehr beobachtbar')}x "
                         "im Reiter 'Jetzt'")

    def test_history_error_is_shown_with_its_text(self):
        """Ein Fehler beim Historienschreiben ist ein Fehler und wird gezeigt."""
        dom = render(sample_state(log={"history_error": "PermissionError: kein Zugriff"}))
        self.assertFalse(hidden(dom, "log-error"))
        self.assertIn("PermissionError: kein Zugriff", dom["log-error"]["text"])

    def test_a_healthy_log_shows_nothing(self):
        dom = render(sample_state(log={"size_mb": 12.0, "warn": False,
                                       "truncated": False, "history_error": None}))
        for ident in ("log-warn", "log-truncated", "log-error"):
            self.assertTrue(hidden(dom, ident), ident)
            self.assertEqual(dom[ident]["text"], "", ident)


@unittest.skipUnless(NODE, "kein node/nodejs im PATH - render() wird nicht ausgefuehrt")
class TestSourceAge(unittest.TestCase):
    """`data.server` wird gelesen: Verbindung und Alter sind sichtbar.

    Spec Z. 313: "Quellen als **getrennte Altersangaben** fuehren. Ist nur eine
    tot, zeigen die uebrigen Kacheln weiter; das Slot-Feld wird grau mit
    'verbunden mit Server: nein'."

    Gebaut wird nur aus `server.connected`, `server.load_error` und
    `server.age_s` - die Felder, die `collector.py` wirklich liefert. Fuer
    `/metrics` und `nvidia-smi` gibt es im Zustand kein Alter, also steht hier
    auch keines.
    """

    def test_slot_tile_reports_a_lost_connection_in_grey(self):
        dom = render(sample_state(server={"connected": False,
                                          "load_error": "ConnectError: [Errno 111]",
                                          "age_s": None}))
        html = dom["k-slot"]["html"]
        self.assertIn("verbunden mit Server: nein", html)
        # Grau ist `--idle`. `--warn` waere "Achtung", nicht "nicht da".
        self.assertIn("var(--idle)", html)
        self.assertNotIn("var(--warn)", html)

    def test_a_connected_slot_keeps_its_state_word(self):
        for state, word in (("busy", "BELEGT"), ("free", "FREI"),
                            ("unknown", "UNBEKANNT")):
            dom = render(sample_state(slot={"state": state},
                                      server={"connected": True}))
            self.assertEqual(dom["k-slot"]["html"],
                             f'<span class="unknown">{word}</span>')

    def test_the_other_tiles_keep_showing_their_values(self):
        """Nur eine Quelle ist tot - die uebrigen Kacheln laufen weiter."""
        dom = render(sample_state(server={"connected": False,
                                          "load_error": "weg", "age_s": None}))
        self.assertEqual(dom["k-gpu"]["html"], "97 %")
        self.assertEqual(dom["k-wait"]["html"], "3")

    def test_a_dead_source_names_its_error_and_invents_no_age(self):
        dom = render(sample_state(server={"connected": False,
                                          "load_error": "ConnectError: [Errno 111]",
                                          "age_s": None}))
        text = dom["source-age"]["text"]
        self.assertIn("ConnectError: [Errno 111]", text)
        self.assertIn("Alter -", text)
        self.assertNotIn("0.0 s", text)

    def test_load_error_is_named_even_when_connected_is_true(self):
        dom = render(sample_state(server={"connected": True,
                                          "load_error": "OSError: kaputt",
                                          "age_s": 0.1}))
        self.assertIn("OSError: kaputt", dom["source-age"]["text"])

    def test_a_fresh_source_shows_its_age_without_a_flag(self):
        dom = render(sample_state(server={"connected": True, "load_error": None,
                                          "age_s": 0.1}))
        text = dom["source-age"]["text"]
        self.assertIn("0.1 s", text)
        self.assertNotIn("veraltet", text)

    def test_an_old_source_keeps_its_number_and_is_flagged(self):
        """Die Zahl bleibt stehen, sie wird nur als veraltet gekennzeichnet.

        Die Schwelle liegt bei drei Poll-Intervallen. Der erste Zustand kennt
        den Takt noch nicht, also gilt der dokumentierte Default von 2 s.
        """
        dom = render(sample_state(server={"connected": True, "load_error": None,
                                          "age_s": 12.0}))
        text = dom["source-age"]["text"]
        self.assertIn("12.0 s", text)
        self.assertIn("veraltet", text)

    def test_the_threshold_follows_the_measured_tick(self):
        """Der Client misst den Takt an `data.at`, statt ihn zu erfinden.

        Drei Zustaende, 10 s auseinander. Der erste kennt den Takt noch nicht
        und rechnet mit dem Default von 2 s (Schwelle 6 s). Danach ist der
        Takt gemessen: die Schwelle liegt bei 30 s. Dasselbe Alter von 12.0 s
        ist also einmal "veraltet" und einmal nicht - mit einer festen Zahl im
        Client waere das nicht moeglich.
        """
        first, second, third = render(
            sample_state(at=1000.0, server={"connected": True, "age_s": 12.0}),
            [sample_state(at=1010.0, server={"connected": True, "age_s": 12.0}),
             sample_state(at=1020.0, server={"connected": True, "age_s": 40.0})])
        self.assertIn("12.0 s", first["source-age"]["text"])
        self.assertIn("veraltet", first["source-age"]["text"])
        self.assertIn("12.0 s", second["source-age"]["text"])
        self.assertNotIn("veraltet", second["source-age"]["text"])
        self.assertIn("40.0 s", third["source-age"]["text"])
        self.assertIn("veraltet", third["source-age"]["text"])


@unittest.skipUnless(NODE, "kein node/nodejs im PATH - render() wird nicht ausgefuehrt")
class TestWaitingFootnote(unittest.TestCase):
    """Fussnote an der WARTEND-Kachel - sichtbar **und** als `title`.

    Spec Z. 245-246: "Der Halter-Wechsel wird erst erkannt, wenn der naechste
    Halter tatsaechlich sendet. Im Extremfall steht der alte Name wenige
    Sekunden zu lang." Die Spec verlangt dafuer ausdruecklich eine Fussnote an
    der WARTEND-Kachel (Z. 252-253); die ersten beiden Punkte der Liste stehen
    laut Spec bereits im Slot-Bereich.

    Beide Orte tragen denselben Satz: der `title` erscheint nur mit der Maus,
    und genau in diesem Zustand schaut niemand auf die Kachel.
    """

    def test_the_note_is_visible_without_a_mouse(self):
        text = render(sample_state())["wait-footnote"]["text"]
        self.assertIn("Halter-Wechsel", text)
        self.assertIn("erst erkannt, wenn der naechste Halter", text)
        self.assertIn("Sekunden zu lang", text)

    def test_the_waiting_tile_carries_it_as_a_title(self):
        title = render(sample_state())["k-wait"]["attrs"]["title"]
        self.assertIn("Halter-Wechsel", title)
        self.assertEqual(title, render(sample_state())["wait-footnote"]["text"])

    def test_the_waiting_number_itself_stays_the_queue_number(self):
        """Die Fussnote darf die Zahl nicht angetasten."""
        dom = render(sample_state(queue={"waiting": 180}))
        self.assertEqual(dom["k-wait"]["html"], "180")
        self.assertIn("Halter-Wechsel", dom["k-wait"]["attrs"]["title"])


class TestQueueFootnote(unittest.TestCase):
    """Der Pflichtsatz zur Nicht-Zuordenbarkeit - ohne erfundene Begruendung.

    Spec Z. 294-296 verlangt **einen Satz**: einzelne Wartende lassen sich
    keinem System zuordnen. Die alte Fassung begruendete das mit einer
    Verbindungszahl, die es seit `36840ab` nicht mehr gibt.
    """

    def test_the_note_says_the_one_thing_the_spec_asks_for(self):
        note = render(sample_state())["queue-note"]["text"]
        self.assertIn("Einzelne Wartende lassen sich keinem System zuordnen",
                      note)
        self.assertIn("eine Zuordnung wird nicht behauptet", note)
        self.assertIn("kein Prompt-Text", note)

    def test_the_note_does_not_mention_a_connection_count(self):
        """Die Begruendung verweist nicht mehr auf ein gestrichenes Element."""
        note = render(sample_state())["queue-note"]["text"]
        self.assertNotIn("Verbindung", note)
        self.assertNotIn("1492", note)
        self.assertNotIn("gemessen", note)


class TestSums(unittest.TestCase):
    """Spec Z. 302: "Summen: Uebergaben, O Dauer, verbleibende Queue" - drei.

    Die vierte Kachel `Tok/s jetzt` war nicht gefordert; der Durchsatz gehoert
    laut Spec Z. 301 in die Sparkline.
    """

    def test_three_sums_and_no_more(self):
        html = read_dashboard()
        tiles = dict((ident, label) for label, ident in
                     re.findall(r'<div class="label"[^>]*>([^<]*)</div>'
                                r'<div class="value" id="([^"]*)">', html))
        self.assertEqual(sorted(i for i in tiles if i.startswith("h-")),
                         ["h-avg", "h-count", "h-queue"])
        self.assertNotIn("h-tps", html)

    @unittest.skipUnless(NODE, "kein node/nodejs im PATH - render() wird nicht ausgefuehrt")
    def test_throughput_is_reachable_only_through_the_sparkline(self):
        """Keine Kachel mehr - und der Durchsatz ist trotzdem zu sehen.

        Die Balkenhoehe ist der Durchsatz, normiert auf den Puffer-Hoechstwert.
        Geprueft wird die **Anzahl** der Balken: eine Zahl im Text gibt es dort
        nicht, und `assertIn("42.5", ...)` waere ein Fehlschluss.
        """
        dom = render(sample_state(speed={"tok_per_s": 42.5}))
        self.assertNotIn("h-tps", dom)
        self.assertEqual(dom["spark"]["html"].count("width:3px"), 1)


class TestSparklineWindow(unittest.TestCase):
    """Die Sparkline zeigt die letzten **60 Sekunden**, nicht 60 Werte.

    Spec Z. 301: "Sparkline tok/s der letzten 60 s". Mit einem Wert je
    `render()` und einem Deckel bei 60 Werten waeren das 120 s - die
    Beschriftung gelogen.
    """

    #: `width:3px` ist die Balkenbreite im Sparkline-Markup. Gezaehlt wird die
    #: **Anzahl**, nicht eine Zahl im Text: `assertIn("2", ...)` ist bei "182"
    #: erfuellt.
    BARS = "width:3px"

    def window(self, *stamps):
        """Puffer mit den angegebenen Unix-Zeiten, je ein Wert `10 + i`."""
        dumps = render(sample_state(at=stamps[0], speed={"tok_per_s": 10.0}),
                       [sample_state(at=stamp, speed={"tok_per_s": 10.0 + index})
                        for index, stamp in enumerate(stamps[1:], start=1)])
        return dumps[-1]["spark"]["html"]

    def test_values_outside_sixty_seconds_fall_out(self):
        base = 1791795000.0
        # Zeitpunkte 0, 40, 75, 80 s. Beim dritten Aufruf ist der erste Wert
        # 75 s alt und faellt raus; 40, 75 und 80 s bleiben stehen.
        html = self.window(base, base + 40, base + 75, base + 80)
        self.assertEqual(html.count(self.BARS), 3)

    def test_values_inside_sixty_seconds_stay(self):
        base = 1791795000.0
        # Genau 60 s ist "innerhalb" - die Grenze gehoert zum Fenster.
        html = self.window(base, base + 59, base + 60)
        self.assertEqual(html.count(self.BARS), 3)

    def test_a_gap_longer_than_the_window_empties_the_history(self):
        """Ein Sprung von 61 s raedt alles, auch den Wert von eben.

        Genau das, was ein zaehlender Puffer nicht kann: er wuerde den alten
        Wert behalten, weil er erst sechzig Eintraege verwerfen muss.
        """
        base = 1791795000.0
        html = self.window(base, base + 61)
        self.assertEqual(html.count(self.BARS), 1)

    def test_an_empty_buffer_shows_a_dash_not_an_empty_box(self):
        dom = render(sample_state(speed={"tok_per_s": None}))
        self.assertNotIn(self.BARS, dom["spark"]["html"])
        self.assertIn("-", dom["spark"]["html"])

    def test_the_label_still_claims_sixty_seconds(self):
        """Die Beschriftung ist erst wahr, seit der Puffer zeitbasiert ist."""
        self.assertIn("Durchsatz letzte 60 Sekunden", read_dashboard())


class TestStreamError(unittest.TestCase):
    """Ein Fehler im SSE-Rahmen darf das Dashboard nicht einfrieren.

    `source.onmessage` rief `render(JSON.parse(event.data))` auch dann auf.
    `data.slot` war `undefined`, `slot.legacy` warf `TypeError`, und die
    Anzeige blieb auf dem letzten guten Tick stehen - ohne jede Altersangabe,
    also genau in dem Fehlerfall, den die Spec mit "Kacheln zeigen Alter statt
    Zahl" beschreibt.
    """

    def test_an_error_frame_keeps_the_last_good_values(self):
        good = sample_state(slot={"state": "busy", "rid": "rid-gut", "system": "Laptop",
                                  "duration_s": 12.0},
                            queue={"waiting": 180}, speed={"tok_per_s": 42.5})
        before, after = render(good, [{"error": "RuntimeError: Poll kaputt"}])
        self.assertEqual(after["k-slot"]["html"], before["k-slot"]["html"])
        self.assertEqual(after["k-wait"]["html"], "180")
        self.assertEqual(after["s-tps"]["text"], "42.5")
        self.assertEqual(after["timeline"]["html"], before["timeline"]["html"])
        self.assertEqual(after["slot-note"]["text"], before["slot-note"]["text"])

    def test_the_failure_is_visible(self):
        _, after = render(sample_state(), [{"error": "RuntimeError: Poll kaputt"}])
        line = after["source-age"]["text"]
        self.assertIn("Poll kaputt", line)
        self.assertIn("fehlgeschlagen", line)

    def test_the_age_of_the_good_value_is_still_shown(self):
        _, after = render(sample_state(), [{"error": "RuntimeError: Poll kaputt"}])
        # Ohne diese Zahl ist nicht zu erkennen, wie alt der gute Wert ist.
        self.assertIn("Letzter Wert", after["source-age"]["text"])

    def test_a_recovered_stream_clears_the_failure(self):
        good = sample_state(server={"connected": True, "load_error": None,
                                    "age_s": 0.1})
        _, _, after = render(good,
                             [{"error": "RuntimeError: Poll kaputt"}, good])
        self.assertNotIn("fehlgeschlagen", after["source-age"]["text"])

    def test_the_age_keeps_counting_while_the_stream_is_silent(self):
        """Ohne SSE-Tick muss die Altersanzeige weiterlaufen.

        Ohne den Timer stuende die Zahl nach dem Fehler auf "0 s" still -
        das waere eine Luege, sobald der gute Wert eine Minute alt ist.
        """
        idle = render(sample_state(), [{"error": "RuntimeError: Poll kaputt"}],
                      run_timers=True)
        self.assertGreaterEqual(idle["#timers"], 1)
        self.assertIn("fehlgeschlagen", idle["source-age"]["text"])
        # Der Timer darf die Kacheln nicht anfassen, nur die Alterszeile.
        self.assertEqual(idle["k-wait"]["html"], "3")

    def test_an_error_before_the_first_state_does_not_throw(self):
        """Der allererste Rahmen kann der Fehler sein - auch das darf nicht
        den Aufruf abbrechen."""
        after = render({"error": "RuntimeError: erster Poll"})
        self.assertIn("erster Poll", after["source-age"]["text"])


@unittest.skipUnless(NODE, "kein node/nodejs im PATH - render() wird nicht ausgefuehrt")
@unittest.skipUnless(NODE, "kein node/nodejs im PATH - render() wird nicht ausgefuehrt")
class TestStateBeforeFirstPoll(CollectorCase):
    """Der Zustand **vor** dem ersten Poll, gerendert.

    Seit dem Ticker im Lifespan (F12) kann der erste `render()`-Aufruf ganz
    normal vor dem ersten Poll stattfinden - der Browser fragt `/api/state`,
    der Server hat noch nicht gepollt. Dieser Zustand ist der haeufigste
    Zustand des Dashboards und der einzige, der bisher ohne Begruendung
    auskam.
    """

    def test_before_the_first_poll_the_altbestand_sentence_stays_away(self):
        state = self.collector.state()
        self.assertEqual(state["slot"]["state"], "unknown")
        self.assertIsNone(state["slot"]["legacy"])
        dom = render(state)
        # "Der Halter ist erst bestimmbar, wenn kein Altbestand mehr in der
        # Queue liegt" waere hier eine Behauptung: es liegt gar kein
        # Altbestand belegt, und der Halter ist noch gar nicht gemessen.
        self.assertNotIn("Halter", dom["slot-note"]["text"])
        self.assertNotIn("Altbestand", now_tab(dom))

    def test_before_the_first_poll_the_reason_is_the_collectors_own(self):
        dom = render(self.collector.state())
        self.assertEqual(dom["slot-note"]["text"],
                         self.collector.state()["slot"]["reason"])
        self.assertNotIn("Halter", dom["timeline"]["html"])


@unittest.skipUnless(NODE, "kein node/nodejs im PATH - render() wird nicht ausgefuehrt")
class TestNoTableInNow(unittest.TestCase):
    """Reiter "Jetzt" fuehrt keine Tabelle.

    Spec Z. 294-296: "**Keine Wartelisten-Tabelle.** Sie ist durch den
    Altbestand-Banner und die WARTEND-Kachel ersetzt. Das Dashboard sagt **in
    einem Satz**, dass sich einzelne Wartende keinem System zuordnen lassen."
    Z. 279-296 zaehlt fuer "Jetzt" kein Table auf.

    Die Tabelle listete abgeschlossene Slots - inhaltlich keine Warteliste,
    aber sie stand an einer Stelle, die die Spec nicht auffuehrt, sie speiste
    sich aus derselben Quelle wie der Zeitstrahl (dieselben Eintraege zweimal)
    und der Pflichtsatz haeng genau an ihr.
    """

    def test_there_is_no_table_and_no_history_rows_in_the_markup(self):
        html = read_dashboard()
        self.assertNotIn("history-rows", html)
        now = html.split('id="tab-now"')[1].split('id="tab-history"')[0]
        self.assertNotIn("<table", now)
        self.assertNotIn("<tbody", now)
        self.assertNotIn("<th", now)

    def test_render_does_not_write_a_history_table(self):
        dom = render(sample_state(history=[entry(system="Laptop")]))
        self.assertNotIn("history-rows", dom)
        self.assertNotIn("Noch keine abgeschlossenen Slots", read_dashboard())

    def test_the_token_numbers_survive_in_the_timeline(self):
        """Sie sind nicht verloren: der Zeitstrahl zeigt prompt, tok und cache.

        Sonst waere die Tabellenspalte einfach weg - und die Spec verlangt
        genau diese Zahlen fuer abgeschlossene Eintraege (Z. 265-267).
        """
        dom = render(sample_state(slot={"state": "free", "complete": True},
                                  history=[entry(prompt_tokens=400,
                                                 completion_tokens=20,
                                                 cached_tokens=12,
                                                 duration_s=4.0)]))
        meta = meta_of(entry_with(dom, "FERTIG")[2])
        self.assertIn("ctx 400", meta)
        self.assertIn("20 tok", meta)
        self.assertIn("12 cache", meta)

    def test_the_queue_footnote_stands_where_the_table_was(self):
        """Der Pflichtsatz zur Nicht-Zuordenbarkeit bleibt im Reiter "Jetzt"."""
        html = read_dashboard()
        now = html.split('id="tab-now"')[1].split('id="tab-history"')[0]
        self.assertIn('id="queue-note"', now)


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