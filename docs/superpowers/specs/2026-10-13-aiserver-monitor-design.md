# KI-Server-Monitor (aiserver-monitor) — Design

**Datum:** 2026-10-13
**Status:** Entwurf zur Review (Design vom Nutzer in Abschnitten freigegeben, UI-Mockups akzeptiert)
**Vorgänger:** `2026-10-07-gpu-slot-monitor-design.md` (slotmon) — wird mit diesem Projekt abgelöst.

**Kurzfassung:** Ein Gateway-Dienst sitzt künftig vor dem Escha-SGLang-Inferenzserver und liefert
(a) transparente OpenAI-API-Weiterleitung mit Streaming, (b) ein Web-Dashboard mit Warteschlangen-
und Live-Einblick je Anfrage, Einzel-/Not-Abbruch und Statistiken mit Verlauf. Der Transport ist so
gebaut, dass Cloudflares Proxy-Read-Timeout den Informationsfluss auch bei langen Warteschlangen
nicht bricht.

---

## 1. Zweck & Auftrag

Der bisherige Monitor (slotmon, Port 8090, Hostname `monitor-aiserver.marechomes.com`) erfüllt weder
Design noch Funktionsumfang. Gewünscht ist ein kompletter Neubau mit fünf Kernpunkten:

1. **Warteschlange sichtbar:** Queue-Zustand (laufend/wartend) als Dashboard.
2. **Live-Einblick:** Zu jedem laufenden Job ist sichtbar, was er gerade produziert
   (Kennzahlen + mitlaufende Textvorschau).
3. **Abbruch:** Einzelne Jobs abbrechbar; zusätzlich „Alles stoppen“.
4. **Statistiken:** Live-Kennzahlen und persistierter Verlauf (Zeitfenster wählbar, neustartfest).
5. **Cloudflare-fest:** Das Timeout der Cloudflare-Strecke darf den Informationsfluss nicht
   unterbrechen — weder bei lange wartenden noch bei lange laufenden Anfragen.

## 2. Harte Randbedingungen & bewusste Abgrenzung

- **Aufhebung einer alten Randbedingung (ausdrücklich freigegeben am 2026-10-13):**
  Die Slotmon-Spec von 2026-10-07 forderte: „der sglang-Server wird nie neugestartet und nie
  verändert, der Monitor liest nur“. Dieses Projekt ändert das bewusst: Der Gateway sitzt
  **im Datenpfad**, und beim Cutover wird der Server-Port getauscht (Details in §8).
- Kein Fork-Patch am SGLang-Code (Ansatz 2 verworfen) — der Server selbst bleibt unverändert.
- Keine Service-Authentifizierung im Monitor (bewusste Nutzerentscheidung; Schutz über Netz- und
  Cloudflare-Ebene).
- Live-Vorschau = gekürzte Textvorschau; kein volles Token-Streaming ins Dashboard und keine
  Persistenz von Vorschautexten.
- Keine Änderungen an bestehenden Clients (hermes, paperclip, OpenCode …): höchstens eine
  Adress-/Konfigzeile; `stream=false` bleibt funktionsfähig.
- Abbrüche laufen ausschließlich über die vorhandene Server-API `/abort_request`
  (Einzel-`rid` bzw. `abort_all`) — es wird nichts erfunden oder nachgebaut.

## 3. Ausgangslage (verifiziert am 2026-10-13)

- **Server:** Escha-SGLang-Fork (`escha 1.2.0+qwen3dense`), Modell `Qwen3.8-27B-Escha-W2`
  (2-bit, ~10 GB), Single-GPU RTX 5070 Ti (16 GB). Start über `runtime/sglang/serve.sh`.
- **serve.sh-Betriebsart:** Default `SERIAL=1` → serielle Abarbeitung mit genau einem GPU-Slot;
  weitere Anfragen warten in der Scheduler-Warteschlange (FIFO). `MAXREQ`, `MAXMAMBA` und
  `CUDA_GRAPH_BS` sind dokumentierte Overrides (explizite Angabe schlägt `SERIAL`).
  JSON-Request-Logging (`request.received`/`request.finished` inkl. rid, ohne Prompt-Inhalte)
  ist per Default aktiv (`ENABLE_LOG_REQUESTS=1`).
  Defaults: `HOST=127.0.0.1`, `PORT=30000`, `API_KEY` optional (Pflicht bei externem Host).
- **Vorhandene Endpunkte (Nutzung durch uns):** Prometheus `/metrics` (aktiv), `/get_load`,
  `/get_server_info`, `POST /abort_request`, `/v1/chat/completions` u. a.
  **Es gibt keinen Live-Token-Endpunkt** — die Live-Vorschau ist der Kern-Baustein dieses Projekts.
- **Aufbau heute:** Der alte Monitor läuft als systemd-User-Unit `slotmon-monitor.service` und ist
  über den bestehenden Cloudflare-Tunnel unter `monitor-aiserver.marechomes.com` erreichbar
  (Tunnel = Token-verwalteter Systemdienst). Er bleibt bis zur Abnahme unangetastet in Betrieb.

## 4. Entscheidungen (Nutzer, im Brainstorming am 2026-10-13)

| Thema | Entscheidung |
|---|---|
| Zielgruppe / Auth | Nutzer + kleine bekannte Gruppe; ausdrücklich **keine** Service-Auth |
| Erreichbarkeit | Bestehender Cloudflare-Tunnel; ersetzt slotmon unter `monitor-aiserver.marechomes.com` |
| Live-Tiefe | Kennzahlen je Job + mitlaufende Textvorschau (kein Token-Streaming) |
| Abbruch | Einzelabbruch ✕ pro Job + „Alles stoppen“ (Zwei-Klick-Bestätigung) |
| Statistiken | Live **und** Verlauf (SQLite), Zeitfenster wählbar, neustartfest |
| Architektur | Ansatz 1: Dienst **vor** dem Server (Gateway), transparente Weiterleitung |
| Rollout | **Variante B: Porttausch** — Gateway übernimmt `:30000`, SGLang intern `:30001` |
| UI | Mockups akzeptiert (Live- und Statistik-Tab wie in der Companion-Session gezeigt) |

## 5. Architektur

Ein Gateway-Prozess (Python 3.12, FastAPI/uvicorn), gegliedert in:

1. **Passthrough** — OpenAI-kompatible Weiterleitung an den SGLang-Port inklusive
   SSE-Streaming; ergänzt um Job-Verfolgung und Keepalives.
2. **Job-Registry** — je Anfrage: rid, Quelle, Modell, Status (wartend → läuft →
   fertig/abgebrochen/Fehler), Zeiten (Start, TTFT, Ende), Tokenzahlen, tok/s,
   Promptanfang (≈100 Zeichen) und Textvorschau (nur RAM).
3. **Metrik-Sammler** — pollt alle ~2 s `/metrics` + `/get_load` (Queue-Tiefe, Durchsatz)
   sowie `nvidia-smi` (VRAM/Auslastung); schreibt Messreihen nach SQLite.
4. **SSE-Hub** — ein Ereignisstrom für die Dashboards: vollständiger Snapshot beim Verbinden,
   danach inkrementelle Updates; Heartbeats (~15 s) auch ohne Ereignis.
5. **Control-API** — Jobs auflisten, Einzelabbruch, Alles-Abbruch (→ `/abort_request`),
   Statistik-Abfragen.
6. **History (SQLite)** — Job-Metadaten + Messreihen; Retention konfigurierbar
   (Vorschlag: Jobs 30 Tage, Messreihen 7 Tage); Vorschauen werden nie persistiert.
7. **Dashboard (React + TypeScript)** — der Build wird vom Gateway ausgeliefert
   → ein Dienst, ein Port, ein Tunnelziel.

**Datenfluss:** Gruppe → Cloudflare → **Gateway :30000** → SGLang `127.0.0.1:30001` (strikt lokal).

**Geplante Routen (Skizze, Details im Implementierungsplan):**

- `POST /v1/chat/completions` (und weitere OpenAI-Routen) — transparent
- `GET /monitor/events` — SSE-Strom fürs Dashboard
- `GET /monitor/jobs` — Snapshot der aktuellen Jobs
- `POST /monitor/jobs/{rid}/cancel` · `POST /monitor/cancel_all`
- `GET /monitor/stats?from=…&to=…` — Verlaufsdaten
- `GET /healthz` — Lebenszeichen
- `/` — Dashboard-Build

## 6. UI (aus den akzeptierten Mockups)

- **Kopfleiste:** Server-Status (verbunden/getrennt, SGLang erreichbar), Modellname, kompakte
  GPU-Anzeige (VRAM/Auslastung), Tabs Live/Statistik, Notaus „Alles stoppen“ (Zwei-Klick).
- **Live-Tab:** Queue-Leiste (laufend/wartend, Durchsatz-Mini-Sparkline); Job-Liste je Anfrage mit
  Status, Quelle (lokal oder echte Besucher-IP aus `CF-Connecting-IP`), Prompt-Anfang, Laufzeit,
  TTFT, Tokens in/out, tok/s, Fehlertext, ✕; Klick öffnet Einblick-Modal mit vollem Prompt,
  „Denken“ (`reasoning_content`, standardmäßig eingeklappt) und „Antwort“ (`content`, mitlaufend);
  Filter Aktiv/Alle.
- **Statistik-Tab:** Zeitfenster 1 h / 24 h / 7 d / 30 d; KPI-Karten; Verlaufs-Charts für
  Queue-Tiefe, Durchsatz/TTFT, Tokens, GPU/VRAM; leichtgewichtiges Charting (z. B. uPlot),
  keine schweren Abhängigkeiten.
- Deutsche UI, Dark Theme, handytaglich.

## 7. Transport & Cloudflare-Robustheit

- **Prinzip:** Fehler 524 entsteht, wenn das Origin zu lange nichts liefert
  (Proxy Read Timeout, Default ~125 s gemäß Doku 2026; historisch „100 s“). Datenfluss vor Ablauf
  hält die Verbindung offen; das gilt nur auf der Cloudflare-Strecke.
- **Dashboard-SSE:** Kommentar-Heartbeats (`: ping`) alle ~15 s auch ohne Ereignis;
  EventSource-Reconnect mit vollem Snapshot (kein Zustandsverlust).
- **Inferenz-Anfragen:** Das Gateway antwortet sofort und sendet während der Wartezeit auf das
  erste Token SSE-Kommentar-Keepalives (~15 s; für SDKs unsichtbar). Beispiel: 4 Minuten
  Wartezeit = 16 Keepalives, weit unter dem Limit. Danach fließt der normale Tokenstrom.
- **`stream=false`-Sonderfall:** Empfehlung bleibt `stream=true`; optionaler Schutz: nach längerer
  Bearbeitung werden vor der JSON-Antwort einzelne Whitespace-Bytes gesendet.
  In allen Fällen: **keine Client-Änderungen nötig**, höchstens eine Adresszeile.

## 8. Rollout (Variante B) & Rollback

**Vorbereitung:** Bauen und lokal gegen Testports testen (altes Setup bleibt unangetastet,
slotmon läuft weiter).

**Cutover (erst nach Abnahme):**
1. `serve.sh` dauerhaft auf `PORT=30001` umstellen; Server neu starten.
2. Gateway auf `0.0.0.0:30000` starten; systemd-User-Unit `aiserver-monitor.service`
   (Auto-Restart, Start beim Boot) aktivieren.
3. Cloudflare-Hostnames prüfen/anpassen: `monitor-aiserver.marechomes.com` → Gateway;
   alles, was auf „:30000“ zeigte, passt automatisch.
4. Abnahmetests inklusive echtem Cloudflare-Test (§10).
5. Danach alten `slotmon-monitor.service` stoppen/deaktivieren.

**Rollback jederzeit:** SGLang zurück auf `PORT=30000`, Gateway stoppen, slotmon wieder starten.

*(Der bisherige `API_KEY`-Schalter von serve.sh bleibt für andere Betriebsarten erhalten; im
Cutover-Betrieb ist der SGLang-Port nur lokal erreichbar und benötigt keinen Schlüssel.)*

## 9. Fehlerverhalten & Betrieb

- **SGLang offline:** Dashboard-Banner + Jobs wechseln in Fehlerzustand; Clients erhalten saubere
  Fehler im OpenAI-Format (kein Hängen).
- **Abbrüche** sind idempotent (doppelter Abbruch = kein Fehler).
- **Gateway-Neustart** bricht Live-Streams ab; Clients reconnecten, Dashboards bauen den Zustand
  aus Snapshot + Metriken neu auf (dokumentierte Grenze; Auto-Restart minimiert sie).
- **Betrieb:** eine Konfigurationsdatei (Ports, Retention, Heartbeat-Intervall, Vorschau-Länge,
  Poll-Intervall, SQLite-/Log-Pfad); Start/Stopp-Skripte; strukturiertes Log mit Rotation;
  `/healthz`; Doku zweisprachig EN/DE wie im Repo üblich.

## 10. Tests & Abnahme

- **Unit:** Job-Registry, Metrik-Aggregation, Retention, Keepalive-Format, Prometheus-Parsing.
- **Integration:** `stream=true/false`, echte Queue-Wartezeit, Abbruch (einzeln/alles;
  wartend/laufend), SGLang-down, SSE-Reconnect.
- **Browser:** Playwright-Tests (Tabs, Einblick-Modal, Abbruch).
- **Cloudflare-Abnahme (echt):** Dashboard-SSE 10 Minuten offen halten (darf nicht abreißen);
  ein Job mit >125 s Gesamtdauer über Cloudflare (antwortet bis zum Ende); Abbruch wirkt live.

## 11. Offene Punkte (vor Cutover zu klären)

1. **Cloudflare-Public-Hostnames + Zielports** — Nutzer liest sie im CF-Dashboard aus;
   der Monitor-Hostname muss beim Cutover aufs Gateway zeigen.
2. **Lokale Konfigurationen mit „30000“** auflisten (Kandidaten: hermes, paperclip, 9Router,
   eigene Skripte) und vor dem Cutover prüfen.
3. **Projektort final bestätigen:** neues Verzeichnis `aiserver-monitor/` im Repo `~/small_qwen`.
4. Im Implementierungsplan gegenprüfen, ob der OpenAI-Pfad eine vom Client vorgegebene `rid`
   durchreicht (Fork-Feld ist optional); Fallback für Abbrüche ist `abort_all`.

## 12. Nicht-Ziele

- Keine Nutzerverwaltung/Login, keine neuen Cloudflare-Dienste (nur der bestehende Tunnel).
- Kein Eingriff in Modell, Quantisierung oder Sampling.
- Keine Archivierung kompletter Dialoge/Prompts (nur Kopf-Anzeige + RAM-Vorschau).
- Der alte slotmon wird ersetzt, nicht erweitert.

## 13. Annahmen

- „Job“ = einzelne API-Anfrage; Batch-Läufe sind viele Jobs.
- Dashboard-Zugriff über den bestehenden Cloudflare-Tunnel.
- Statistiken umfassen Durchsatz, Latenz (TTFT und Gesamtdauer), Tokenverbrauch,
  Queue-Verlauf und GPU-Auslastung/VRAM.
