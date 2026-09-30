<p align="right"><b>🇩🇪 Deutsch</b> | <a href="README.md">🇬🇧 English</a></p>

# Escha Runtime für Qwen3.8-27B-Escha-W2 — SGLang-Adaptionen

Dieses Verzeichnis ist die **Laufzeitumgebung** für das 2-Bit-quantisierte
Modell [`EschaLabs/Qwen3.8-27B-Escha-W2`](https://huggingface.co/EschaLabs/Qwen3.8-27B-Escha-W2):
das `escha`-Wheel mit gepatchtem SGLang-Fork, der Startskript `serve.sh`,
Hilfsskripte für Custom-Logit-Processor-Payloads (**Thinking-Budget** und
**Overthinking-Penalty**) sowie die zugehörige Dokumentation.

Kernstück dieser Adaption ist eine **training-freie Overthinking-Penalty**, die
quantisierte Reasoning-Modelle daran hindert, sich in ihrer eigenen
Gedankenkette zu verstricken — serverseitig per Startflag (`OTP=1`) oder
pro Request steuerbar.

## Inhaltsverzeichnis

1. [Was liegt hier?](#1-was-liegt-hier)
2. [Credits & wissenschaftliche Grundlage](#2-credits--wissenschaftliche-grundlage)
3. [Hintergrund: Overthinking unter Quantisierung](#3-hintergrund-overthinking-unter-quantisierung)
4. [Die Lösung: Logit-Penalty auf Zögerungsmarkern](#4-die-lösung-logit-penalty-auf-zögerungsmarkern)
5. [Funktionsweise im Detail](#5-funktionsweise-im-detail)
6. [Schnellstart](#6-schnellstart)
7. [Serverstart im Detail (`serve.sh`)](#7-serverstart-im-detail-servesh)
8. [Server-Default-Penalty (`OTP=1`)](#8-server-default-penalty-otp1)
9. [Requests senden & Rezepte](#9-requests-senden--rezepte)
10. [Wirkung messen](#10-wirkung-messen)
11. [Verifikation](#11-verifikation)
12. [Grenzen & Hinweise](#12-grenzen--hinweise)
13. [Weiterführende Dokumentation](#13-weiterführende-dokumentation)

---

## 1. Was liegt hier?

| Datei | Zweck | Status |
|---|---|---|
| `escha-1.2.0+qwen3dense-cp312-*.whl` | Runtime-Wheel: `escha` + SGLang-Fork | original |
| `serve.sh` | Server-Launcher; alle Stellschrauben als Env-Vars | original (Details s. Dateikopf + [`INSTALL.md`](INSTALL.md)) |
| `start-penalty.sh` / `stop-server.sh` | Server im Hintergrund starten (Penalty an) bzw. stoppen | Hilfsskripte |
| `INSTALL.md` | Installation, GPU-Profile, Troubleshooting | original |
| `thinking_budget.py` | Payload-Builder für Thinking-Budget | original (Helper) |
| `overthinking_penalty.py` | **Payload-Builder für Overthinking-Penalty** | **NEU — Kern-Adaption** |
| `bench_with_penalty.py` | Benchmark-Wrapper: `sglang.bench_serving` mit Penalty-Payload in jedem Request | **NEU** |
| `DESIGN-server-default-penalty.md` | Design-Dokument des Server-Default-Patches (Code-Anker, Patchpunkte A–D) | **NEU** |
| `patches/server-default-penalty.patch` | Patch gegen den Fork: `OTP=1` injiziert die Penalty serverseitig | **NEU** |
| `examples/penalty_example.py` | Beispiel-Client gegen einen `OTP=1`-Server: Default-Injektion, Override λ=0 / λ=4, Kombi mit Budget | **NEU** |
| `USAGE-penalty.md` | **Detaillierte Bedienungsanleitung**: beide Aktivierungswege, curl-/Python-Beispiele, Wirkung messen, Troubleshooting | **NEU** |
| `README.md` / `README.de.md` | diese Datei (EN/DE) | **NEU** |

---

## 2. Credits & wissenschaftliche Grundlage

- **Modell & Runtime-Grundlage:** [`EschaLabs/Qwen3.8-27B-Escha-W2`](https://huggingface.co/EschaLabs/Qwen3.8-27B-Escha-W2)
  — das 2-Bit-quantisierte Checkpoint (escha-Methode, ~10 GB), das Wheel
  `escha-1.2.0+qwen3dense` und der ursprüngliche `serve.sh`-Launcher stammen aus dieser
  Distribution. Alle Leistungen rund um Quantisierung, Kernels und Benchmark-Profile
  gehen an EschaLabs.
- **Wissenschaftliche Grundlage der Overthinking-Penalty:**
  Lotfi, Kirichenko, Li, Liu (Meta FAIR):
  *"Quantized Reasoning Models Think They Need to Think Longer, but They Do Not"*
  ([arXiv:2606.00206](https://arxiv.org/abs/2606.00206)). Die Marker-Liste, die
  Penalty-Formel und die λ-Empfehlungen stammen aus dem Paper (Appendix C, Table 8).
- Die Implementierung hier (`overthinking_penalty.py`, Server-Patch) ist eine
  **lokale Adaption** für genau diesen Fork/Checkpoint; Fehler in der Adaption gehen
  auf uns, nicht auf EschaLabs oder die Paper-Autoren.

---

## 3. Hintergrund: Overthinking unter Quantisierung

Post-Training-Quantisierung (PTQ) beschädigt bei Reasoning-Modellen nicht primär die
Antwortqualität — sondern das **Terminierungsverhalten der Gedankenkette**. Das Paper
zeigt:

- PTQ-quantisierte Reasoning-Modelle finden die richtige Antwort oft schon *mitten*
  in der Chain-of-Thought …
- … sampeln dann aber **Zögerungsmarker** („Wait“, „But“, „Alternatively“), öffnen
  neue Denkzweige und reden sich aus der bereits gefundenen Antwort heraus.
- Folgen: CoT-Länge bis **4,5×**, Accuracy bricht ein (AWQ W3 auf MATH-500:
  85,6 % → 47,0 %).

Genau dieses Muster ist auf diesem W2-Export sichtbar: Reasoning-Traces laufen bis
ans Token-Budget, Antworten enden mit `finish_reason: "length"` und leerem
`content: null` — ein Eval-Harness wertet das als **falsch**, nicht als langsam;
ein Agent-Framework sieht einen No-op.

---

## 4. Die Lösung: Logit-Penalty auf Zögerungsmarkern

An **jedem** Dekodierschritt wird von den Logits aller 49 Markertokens ein fester
Wert λ abgezogen (`z′(v) = z(v) − λ`):

- **Kein Training, kein Fine-Tuning, kein Extra-Forward-Pass** — der Eingriff
  passiert direkt auf den Logits des normalen Dekodierschritts; es gibt daher
  keinen messbaren Overhead.
- Paper-Ergebnisse: CoT-Länge **−12…−23 %**, Accuracy gleich oder besser
  (+bis 6,2 pp bei AWQ W3), Overthinking-Fehler bis **−58 %**.
- Der Scope ist steuerbar: standardmäßig nur **innerhalb** eines offenen
  `<think>`-Blocks (finale Antwort und Code bleiben unangetastet); optional
  paper-treu an jedem Schritt (`--always`).

---

## 5. Funktionsweise im Detail

### 5.1 Architektur in drei Schritten

1. **Payload-Bau (offline, einmalig):** `overthinking_penalty.py` löst die Marker
   gegen die Tokens des *eigenen* Modells auf, definiert eine Processor-Klasse mit
   eingebackenen Defaults und serialisiert sie per dill als hex-JSON nach stdout —
   exakt dasselbe Transportformat wie `thinking_budget.py`. Der Server lädt die
   Klasse zur Laufzeit über den SGLang-Mechanismus `custom_logit_processor`
   (`--enable-custom-logit-processor`; `serve.sh` setzt das via `ENABLE_CLP=1`
   **standardmäßig**).
2. **Request-Seite:** Der Client schickt zwei SGLang-Erweiterungsfelder:
   `"custom_logit_processor": "<payload>"` und optional `"custom_params": {...}`.
   Der Fork übergibt pro Batch-Zeile ein Param-Dict an
   `processor.__call__(logits, custom_param_list)` und injiziert automatisch das
   Req-Objekt unter `"__req__"` (State-Tracking: `origin_input_ids` + `output_ids`,
   siehe `schedule_batch.py`).
3. **Pro Batch-Zeile und Dekodierschritt:** erst ggf. Budget-Zwangsschluss
   (Newline erzwingen, dann `</think>`), sonst Penalty.

### 5.2 Token-Recherche (am echten Checkpoint verifiziert)

- Auflösung ist **encode-basiert**: `tok.encode(marker, add_special_tokens=False)`
  mit Round-Trip-Check (`decode(ids) == marker`). `convert_tokens_to_ids()`
  liefert bei diesem ~248k-Vokabular für Klartext-Marker nur `None` — der
  naheliegende Weg funktioniert hier also gerade *nicht*.
- **49 von 50** Paper-Markern sind Einzeltokens mit exaktem Round-Trip. Einzig
  `" recheck"` zerfällt in `[311, 1960]` → wird übersprungen (mit Warnung auf
  stderr). Beispiele: `" perhaps"`→8106, `" maybe"`→6970, `" wait"`→3655,
  `" Wait"`→13428, `" But"`→1921, `" alternatively"`→66073, `" wrong"`→4808.
- Denkankerntokens: `<think>`=248068, `</think>`=248069, Newline=`198` — identisch
  zu den Konstanten des Forks (`Qwen38ThinkingBudgetLogitProcessor`). Sie werden
  zur Build-Zeit vom Tokenizer gelesen, nicht hartkodiert.

### 5.3 Parameter-Vertrag (`custom_params`, alle Keys OPTIONAL)

| Key | Typ | Bedeutung | Fallback |
|---|---|---|---|
| `penalty_lambda` | float | Abzug pro Markertoken und Schritt. **`0` schaltet die Penalty komplett ab.** | CLI `--lambda` (im Payload eingebacken), Default 2.0 |
| `think_only` | bool | `true`: Penalty **nur innerhalb** eines offenen `<think>`-Blocks (finale Antwort/Code bleiben unangetastet). `false`: an jedem Schritt (paper-treu). | CLI `--think-only`/`--always` (eingebacken), Default `true` |
| `thinking_budget` | int | Max. Reasoning-Tokens nach `<think>`; danach erzwingt der Processor erst Newline, dann `</think>`. Nicht gesetzt = aus. | — (aus) |

**Präzedenzkette:**

```
Request-custom_params  >  CLI-Defaults (im Payload eingebacken)  >  eingebaute Fallbacks
```

Bei `OTP=1` (Server-Default) gilt dieselbe Kette — der injizierte Payload ist
nur der unterste Fallback, ein Request kann alles überschreiben.

**Warum Penalty und Budget in einer Klasse stecken:** SGLang erlaubt nur **einen**
`custom_logit_processor`-String pro Request. Deshalb fasst dieser Processor beide
Hebel zusammen — beide sind in einem Request nutzbar:

```jsonc
{ "custom_logit_processor": "<otp.json>",
  "custom_params": {"penalty_lambda": 2.0, "think_only": true, "thinking_budget": 28672} }
```

**Degrade-Verhalten (bewusst so gebaut):**

- Fehlt `__req__` (z. B. weil `custom_params` ganz weggelassen wurde), kann der
  Think-Scope nicht geprüft werden — dann penaltyiert der Processor *immer*,
  statt die Zeile still zu überspringen (kein stummes Nichtstun).
- Ungültiges `penalty_lambda` (z. B. String) → Zeile wird übersprungen, kein Crash.
- λ = 0 → Penalty aus; ein gesetztes `thinking_budget` bleibt trotzdem aktiv.

### 5.4 CLI-Flags von `overthinking_penalty.py`

| Flag | Bedeutung | Default |
|---|---|---|
| `--model DIR` | Modellordner (Tokenizer); Pflicht | — |
| `--lambda FLOAT`, `-l` | eingebackener Default-λ; Paper-Sweep 0.5–4.0 | `2.0` |
| `--think-only` / `--always` | eingebackener Default-Scope (exklusiv) | `--think-only` |
| `--demo` | Gegen laufenden Server testen: off vs. default vs. λ=4 | aus |
| `--base-url URL` | Endpoint für `--demo` | `http://127.0.0.1:30000/v1` |
| `--served-name NAME` | Model-ID für `--demo` | `escha-qwen38-27b-w2` |
| `--max-tokens N` | Demo-Budget | `4096` |

---

## 6. Schnellstart

Vier Schritte von null zum laufenden Server mit Penalty:

```bash
cd /home/user/smal_qwen

# 1) Payload bauen (einmalig; Defaults: λ=2.0, think-only)
.venv/bin/python runtime/sglang/overthinking_penalty.py \
    --model ./Qwen3.8-27B-Escha-W2 > otp.json

# 2) Server MIT Penalty als Serverdefault starten (16-GB-Profil):
MODEL=./Qwen3.8-27B-Escha-W2 VENV=.venv \
    MEM=0.90 CTXLEN=16384 CUDA_GRAPH_BS='1 2 4 8' OTP=1 \
    bash runtime/sglang/serve.sh

# 3) Im Startlog muss erscheinen:
#    [serve] ... OTP=1(lam=2.0)
#    default overthinking penalty active (lambda=2.0, think_only=True)

# 4) Aus einem ZWEITEN Terminal fragen — plain OpenAI-Request, KEIN Payload nötig:
curl -s http://127.0.0.1:30000/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model": "escha-qwen38-27b-w2",
         "messages": [{"role": "user", "content": "Was ist 17*24? Denk kurz."}],
         "max_tokens": 512, "temperature": 0.6}' | head -c 600
```

stderr von Schritt 1 meldet die Token-Auflösung
(`# resolved 49/49 overthinking markers`).
Alle Varianten, der rohe CLI-Weg und Troubleshooting stehen in
[`USAGE-penalty.md`](USAGE-penalty.md).

---

## 7. Serverstart im Detail (`serve.sh`)

Beispielaufruf aus dem Repo-Root (loggt nach `/tmp/opencode/server.log`):

```bash
setsid nohup env MODEL=./Qwen3.8-27B-Escha-W2 VENV=.venv \
    MEM=0.92 CTXLEN=100000 HOST=0.0.0.0 API_KEY=marec \
    CUDA_GRAPH_BS='1 2 4' EXTRA_ARGS="--enable-metrics" \
    bash runtime/sglang/serve.sh >/tmp/opencode/server.log 2>&1 &
```

Die wichtigsten Env-Vars (vollständige Liste im Skriptkopf):

| Var | Bedeutung |
|---|---|
| `MODEL` | Modellordner (Pflicht) |
| `VENV` | zu aktivierendes venv (hier `.venv` im Repo) |
| `MEM` | `--mem-fraction-static` (KV-Pool-Größe) |
| `CTXLEN` | Kontext-Cap pro Request |
| `HOST` / `PORT` | Bind-Adresse (Default 127.0.0.1:30000); bei `0.0.0.0` zwingend `API_KEY` setzen! |
| `API_KEY` | Bearer-Key für Clients |
| `CUDA_GRAPH_BS` | erfasste CUDA-Graph-Batchgrößen |
| `SERIAL` | `1` (Default) = single-flight: eine laufende Generation gleichzeitig, weitere Requests warten in der FIFO-Schlange des Servers (kein 429/503, kein paralleles GPU-Decode) — das VRAM-sichere Profil für Multi-Agenten-Clients; `0` = auto/batched. S. [INSTALL.de.md](INSTALL.de.md#single-flight-modus-f%C3%BCr-multi-agenten-clients-default) |
| `EXTRA_ARGS` | rohe Zusatz-Flags für `launch_server` (z. B. `--enable-metrics`) |
| `ENABLE_CLP` | `1` (= Default) → `--enable-custom-logit-processor` — **Voraussetzung für Penalty UND Budget** |
| `OTP` | `1` → Serverdefault-Penalty aktiv (siehe nächster Abschnitt) |
| `PENALTY_LAMBDA` | Default-λ des injizierten Payloads (`2.0`) — nur relevant bei `OTP=1` |
| `PENALTY_ALWAYS` | `1` → jeder Token statt nur `<think>`; Default `0` = think-only |
| `KVDTYPE` | KV-Cache-Datentyp, Default `fp8_e4m3`; s. [`docs/kv-cache-quantisierung.md`](../../docs/kv-cache-quantisierung.md) |

GPU-abhängige Profile (welche Werte für welche Karte) behandelt
[`INSTALL.md`](INSTALL.md), Abschnitt *Running on your GPU*.

---

## 8. Server-Default-Penalty (`OTP=1`)

Seit dem Patch `patches/server-default-penalty.patch` baut der Server beim Start
selbst einen Penalty-Payload — gleicher Processor, gleiche Marker, nur ohne den
CLI-Umweg. Anwendbar mit `git apply` aus dem venv-Root; die Code-Anker und
Patchpunkte A–D sind in [`DESIGN-server-default-penalty.md`](DESIGN-server-default-penalty.md)
dokumentiert.

```bash
MODEL=./Qwen3.8-27B-Escha-W2 OTP=1 bash runtime/sglang/serve.sh
# Startlog: "default overthinking penalty active (lambda=2.0, think_only=True)"
```

Eigenschaften:

- **Wirkt auf** chat/completions, completions und `/generate` — jeder Request
  ohne eigenes `custom_logit_processor`. Requests **mit** eigenem Payload bleiben
  unangetastet.
- **Vorrang unverändert**: request-`custom_params` > Server-Default > Fallback.
  Pro Request abschalten: `"custom_params": {"penalty_lambda": 0}`;
  kombinieren: `{"penalty_lambda": 4.0, "thinking_budget": 28672}`.
- `OTP=1` ohne `ENABLE_CLP=1` bricht in `serve.sh` mit Fehler ab (der injizierte
  Payload wäre sonst ein Custom-Logit-Processor auf einem Server, der die ablehnt).
- **Deaktiviert = Default:** ohne `OTP=1` verhält sich der Server bit-identisch
  zum ungepatchten Stand.
- Bei fehlgeschlagenem Payload-Build (z. B. Tokenizer ohne `<think>`) loggt der
  Server `default overthinking penalty disabled: <Grund>` und läuft **normal
  weiter** — nur ohne Default-Penalty.

Laufendes Beispiel (alle vier Varianten gegen einen `OTP=1`-Server):

```bash
.venv/bin/python runtime/sglang/examples/penalty_example.py [BASE_URL]
```

---

## 9. Requests senden & Rezepte

### 9.1 Mit eigenem Payload (Weg A)

```bash
CLP=$(cat otp.json)
python3 - "$CLP" <<'EOF'
import json, sys, urllib.request
clp = sys.argv[1]
body = {
    "model": "escha-qwen38-27b-w2",
    "messages": [{"role": "user",
                  "content": "What is the largest eight-digit base 2 integer? "
                             "Express your answer in base 10."}],
    "max_tokens": 8192, "temperature": 0.6, "top_p": 0.95,
    # SGLang-Erweiterungen:
    "custom_logit_processor": clp,
    "custom_params": {"penalty_lambda": 2.0, "think_only": True},
}
req = urllib.request.Request("http://127.0.0.1:30000/v1/chat/completions",
                             json.dumps(body).encode(),
                             {"Content-Type": "application/json",
                              "Authorization": "Bearer marec"})
print(json.load(urllib.request.urlopen(req, timeout=3600))["choices"][0]
      ["message"]["content"])
EOF
```

### 9.2 Rezepttabelle (`custom_params`)

| Ziel | `custom_params` |
|---|---|
| Default (λ=2, nur im Denken) | `{}` weglassen oder `{}` |
| Stärker / paper-treu überall | `{"penalty_lambda": 4.0, "think_only": false}` |
| **Komplett abschalten** (pro Request) | `{"penalty_lambda": 0}` |
| Nur Deckel auf dem Denken | `{"penalty_lambda": 0, "thinking_budget": 28672}` |
| Beides kombiniert | `{"penalty_lambda": 2.0, "think_only": true, "thinking_budget": 28672}` |

Zur λ-Wahl: **2.0** ist der Paper-Default und auf DIESEM Modell gemessen.
Über 2.5 macht das Reasoning deutlich knapper; über 4.0 riskiert man, dass
legitime Umplanung („wait“, „but“) unterdrückt wird. Zum Ausprobieren genügt ein
Request-Override — ein Neustart ist dafür NICHT nötig.

---

## 10. Wirkung messen

Drei Wege, den Effekt zu sehen:

1. **Log-Zeile** beim Serverstart: `default overthinking penalty active (…)`.
2. **Demo-Vergleich** gegen einen laufenden Server — sendet off/default/stark und
   tabelliert `finish_reason`, `completion_tokens`, `reasoning_chars`,
   `answer_chars`:
   ```bash
   .venv/bin/python runtime/sglang/overthinking_penalty.py \
       --model ./Qwen3.8-27B-Escha-W2 --demo
   ```
   Erwartbarer Effekt: weniger Reasoning-Zeichen, öfter `stop` statt `length`.
3. **Benchmark mit Penalty:** `bench_with_penalty.py` injiziert den Payload in
   **jeden** Request eines `sglang.bench_serving`-Laufs (Off-Lauf: `--lambda 0`):
   ```bash
   .venv/bin/python runtime/sglang/bench_with_penalty.py \
       --model-dir ./Qwen3.8-27B-Escha-W2 --lambda 2.0 --think-only \
       --dataset-name random --num-prompts 100
   ```

A/B-Tipp: denselben Prompt einmal mit `"penalty_lambda": 0` und einmal mit dem
Default senden und `finish_reason` + Token-Zahlen vergleichen.

---

## 11. Verifikation

Durchgeführt an diesem Fork/Checkpoint:

- `py_compile` sauber; Payload ist valides JSON, dill-Roundtrip lädt die Klasse
  (49 Tokens, Think-IDs 248068/248069, Defaults korrekt eingebacken).
- Logiktests mit Fake-Reqs/Tensoren: inside-think bestraft, closed-think/nicht-think
  unberührt (think_only), `λ=0` aus, `{}`-Params → Defaults, `think_only:false` →
  always-on, Request-λ-Override (z. B. 4.5), Garbage-λ ohne Crash, leere/None-
  Paramliste unverändert.
- Budget-Tests: Budget überschritten + letztes Token ≠ Newline → Newline erzwungen;
  letztes Token = Newline → `</think>` erzwungen; unter Budget → unangetastet;
  gemischter Batch (Zeile A Maskierung, Zeile B Penalty) korrekt getrennt.
- Token-Auflösung am echten Tokenizer: 49/49, `" recheck"` sauber übersprungen.
- Server-Patch verifiziert gegen den Live-Server (Startlog, Override λ=0/λ=4,
  Batch n>1) — siehe [`DESIGN-server-default-penalty.md`](DESIGN-server-default-penalty.md).

---

## 12. Grenzen & Hinweise

- Die Felder `custom_logit_processor`/`custom_params` sind **SGLang-Erweiterungen** —
  Standard-OpenAI-Clients ignorieren sie; LM Studio/opencode etc. nutzen sie nicht
  automatisch (eigene Requests nötig). Der Normalfall ist deshalb `OTP=1`: dann
  brauchen Clients gar nichts zu senden.
- dill serialisiert die Klasse by-value; der Server muss denselben SGLang-Fork
  (gleiches Wheel) fahren wie der Payload-Builder — hier gegeben, da beide `.venv`
  bzw. dieses Wheel nutzen.
- Die Marker wirken nur als **Einzeltokens**: deshalb fehlt `" recheck"` (2 Tokens).
- Negative `penalty_lambda` wäre technisch möglich (Boost) und entspricht der
  Negativ-Kontrollbedingung des Papers — nicht empfohlen für Produktion.

---

## 13. Weiterführende Dokumentation

| Dokument | Inhalt |
|---|---|
| [`USAGE-penalty.md`](USAGE-penalty.md) / [DE](USAGE-penalty.de.md) | Schritt-für-Schritt-Bedienung: beide Aktivierungswege, Client-Beispiele, Wirkung messen, Troubleshooting/FAQ, kompakte Flag-Referenz |
| [`INSTALL.md`](INSTALL.md) / [DE](INSTALL.de.md) | Installation (Anforderungen, die drei still failenden), GPU-Profile nach Compute Capability & VRAM, Troubleshooting |
| [`DESIGN-server-default-penalty.md`](DESIGN-server-default-penalty.md) / [DE](DESIGN-server-default-penalty.de.md) | Architektur-Entscheidung des Server-Patches: verifizierte Code-Anker, Patchpunkte A–D, Edge-Cases, Rollback |
| [`../docs/kv-cache-quantisierung.md`](../../docs/kv-cache-quantisierung.md) | FP8-KV-Cache: Recherche im Fork, Messungen (2× Kontext), Qualitäts-A/B, HiCache-Ausfall |

