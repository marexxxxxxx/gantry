"""Tests fuer den Dashboard-Server: Routen, Zustand, HTML.

Das Dashboard ist eine HTML-Datei mit eingebettetem JavaScript. Fuer die
Verdrahtung von `slot.reason` bis ins Dokument wird der `<script>`-Block
herausgezogen und unter `node` mit einem DOM-Stub ausgefuehrt - der Test
prueft damit, was der Nutzer sieht, nicht die Buchstaben im Quelltext.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from fastapi.testclient import TestClient

from slotmon.collector import Collector
from slotmon.config import load_config
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