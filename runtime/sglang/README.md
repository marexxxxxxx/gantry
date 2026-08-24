# Escha Runtime für Qwen3.8-27B-Escha-W2 — SGLang-Adaptionen

Dieses Verzeichnis ist die **Laufzeitumgebung**: das `escha`-Wheel mit dem gepatchten
SGLang-Fork, der Startskript `serve.sh`, zwei Hilfsskripte zum Bau von
Custom-Logit-Processor-Payloads (`thinking_budget.py`, **neu:** `overthinking_penalty.py`)
und diese Dokumentation.

## Danksagung / Credits

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
- Die Implementierung hier (`overthinking_penalty.py`) ist eine **lokale Adaption**
  für genau diesen Fork/Checkpoint; Fehler in der Adaption gehen auf uns, nicht auf
  EschaLabs oder die Paper-Autoren.

## Inhalt

| Datei | Zweck | Status |
|---|---|---|
| `escha-1.2.0+qwen3dense-cp312-*.whl` | Runtime-Wheel (escha + SGLang-Fork) | original |
| `serve.sh` | Server-Launcher, alle Knobs als Env-Vars | original (Details s. Dateikopf + `INSTALL.md`) |
| `INSTALL.md` | Installation, GPU-Profiles, Troubleshooting | original |
| `thinking_budget.py` | Payload-Builder für Thinking-Budget | original (Helper) |
| `overthinking_penalty.py` | **Payload-Builder für Overthinking-Penalty** | **NEU — Kern-Adaption** |
| `bench_with_penalty.py` | Wrapper: `bench_serving` mit Penalty-Payload via `--extra-request-body` | **NEU** |
| `PLAN-server-default-penalty.md` | Design/Anchors des Server-Default-Penalty-Patches | **NEU** |
| `patches/server-default-penalty.patch` | Patch (server_args, tokenizer_manager, neues Sampling-Modul): `OTP=1` injiziert den Penalty serverseitig | **NEU** |
| `examples/penalty_example.py` | Beispiel-Client für einen `OTP=1`-Server: Default-Injektion, Override λ=0 / λ=4, Kombi mit Budget | **NEU** |
| `README.md` | diese Datei | **NEU** |

---

## Das Problem: Overthinking unter aggressiver Quantisierung

Das Paper zeigt: PTQ-quantisierte Reasoning-Modelle finden die richtige Antwort oft
*mitten* in der Chain-of-Thought — sampeln dann aber Zögerungsmarker („Wait", „But",
„Alternatively"), öffnen neue Denkzweige und reden sich aus der bereits gefundenen
Antwort heraus. Folgen: CoT-Länge bis **4,5×**, Accuracy bricht ein (AWQ W3 auf
MATH-500: 85,6 % → 47,0 %).

Genau das ist auf diesem W2-Export sichtbar: Reasoning-Traces laufen bis ans
Token-Budget, Antworten enden mit `finish_reason: "length"` und `content: null`.

## Die Lösung: Logit-Penalty auf Zögerungsmarkern (training-frei)

An **jedem** Dekodierschritt wird von den Logits aller 49 Markertokens ein fester
Wert λ abgezogen (`z'(v) = z(v) − λ`). Kein Training, kein Extra-Forward-Pass,
kein messbarer Overhead. Paper-Ergebnisse: CoT −12…−23 %, Accuracy gleich oder
besser (+bis 6,2 pp bei AWQ W3), Overthinking-Fehler bis −58 %.

---

## Adaption im Detail: `overthinking_penalty.py`

### Mechanismus

1. **Payload-Bau (offline, einmalig):** Das Skript löst die Marker gegen die Tokens
   des *eigenen* Modells auf, definiert eine Processor-Klasse mit eingebackenen
   Defaults und serialisiert sie per dill als hex-JSON nach stdout — exakt dasselbe
   Transportformat wie `thinking_budget.py`. Der Server lädt die Klasse zur Laufzeit
   über den SGLang-Mechanismus `custom_logit_processor`
   (`--enable-custom-logit-processor`; `serve.sh` setzt das via `ENABLE_CLP=1`
   **standardmäßig**).
2. **Request-Seite:** Der Client schickt zwei SGLang-Erweiterungsfelder:
   `"custom_logit_processor": "<payload>"` und optional
   `"custom_params": {...}`. Der Fork übergibt pro Batch-Zeile ein Param-Dict an
   `processor.__call__(logits, custom_param_list)` und injiziert automatisch das
   Req-Objekt unter `"__req__"` (State-Tracking: `origin_input_ids` + `output_ids`,
   siehe `schedule_batch.py`).
3. **Pro Zeile:** erst ggf. Budget-Zwangsschluss (Teil 1), sonst Penalty (Teil 2).

### Token-Recherche (am echten Checkpoint verifiziert)

- Auflösung ist **encode-basiert**: `tok.encode(marker, add_special_tokens=False)`
  mit Round-Trip-Check (`decode(ids) == marker`). `convert_tokens_to_ids()`
  liefert bei diesem ~248k-Vokabular für Klartext-Marker nur `None` — der naheliegende
  Weg funktioniert also gerade nicht.
- **49 von 50** Paper-Markern sind hier Einzeltokens mit exaktem Round-Trip.
  Einzig `" recheck"` zerfällt in `[311, 1960]` → wird übersprungen (mit Warnung auf
  stderr). Beispiele: `" perhaps"→8106, " maybe"→6970, " wait"→3655,
  " Wait"→13428, " But"→1921, " alternatively"→66073, " wrong"→4808`.
- Denkankerntokens: `<think>=248068`, `</think>=248069`, Newline=`198` — identisch zu
  den Konstanten des Forks (`Qwen38ThinkingBudgetLogitProcessor`). Sie werden zur
  Build-Zeit vom Tokenizer gelesen, nicht hartkodiert.

### Parameter-Vertrag (`custom_params`, alle Keys OPTIONAL)

| Key | Typ | Bedeutung | Fallback |
|---|---|---|---|
| `penalty_lambda` | float | Abzug pro Markertoken und Schritt. **`0` schaltet die Penalty komplett ab.** | CLI `--lambda` (im Payload eingebacken), Default 2.0 |
| `think_only` | bool | `true`: Penalty **nur innerhalb** eines offenen `<think>`-Blocks (finale Antwort/Code bleiben unangetastet). `false`: an jedem Schritt (paper-treu). | CLI `--think-only`/`--always` (eingebacken), Default `true` |
| `thinking_budget` | int | Max. Reasoning-Tokens nach `<think>`; danach erzwingt der Processor (wie der Stock-Processor) erst Newline, dann `</think>`. Nicht gesetzt = aus. | — (aus) |

**Präzedenz:** Request-`custom_params` > CLI-Default im Payload > eingebauter Fallback.

**Kombination:** SGLang erlaubt nur **einen** `custom_logit_processor`-String pro
Request — deshalb fasst dieser Processor **Penalty UND Thinking-Budget in einer
Klasse** zusammen. Beide Hebel sind in einem Request nutzbar:

```jsonc
{ "custom_logit_processor": "<otp.json>",
  "custom_params": {"penalty_lambda": 2.0, "think_only": true, "thinking_budget": 28672} }
```

**Degrade-Verhalten:** Fehlt `__req__` (z. B. weil `custom_params` ganz weggelassen
wurde), kann der Think-Scope nicht geprüft werden — dann penaltyiert der Processor
*immer*, statt die Zeile still zu überspringen (kein stummes Nichtstun).
Ungültiges `penalty_lambda` (z. B. String) → Zeile wird übersprungen, kein Crash.

### CLI-Flags

| Flag | Bedeutung | Default |
|---|---|---|
| `--model DIR` | Modellordner (Tokenizer); Pflicht | — |
| `--lambda FLOAT`, `-l` | eingebackener Default-λ; Paper-Sweep 0.5–4.0 | `2.0` |
| `--think-only` / `--always` | eingebackener Default-Scope (mutually exclusive) | `--think-only` |
| `--demo` | Gegen laufenden Server testen: off vs. default vs. λ=4 | aus |
| `--base-url URL` | Endpoint für `--demo` | `http://127.0.0.1:30000/v1` |
| `--served-name NAME` | Model-ID für `--demo` | `escha-qwen38-27b-w2` |
| `--max-tokens N` | Demo-Budget | `4096` |

---

## Benutzung

### 1. Payload bauen (einmalig)

```bash
cd /home/user/smal_qwen
.venv/bin/python runtime/sglang/overthinking_penalty.py \
    --model ./Qwen3.8-27B-Escha-W2 > otp.json          # Defaults: λ=2.0, think-only
# Varianten:
#   ... --lambda 4.0 --always > otp_always.json        # paper-treu, stärker
```

stderr meldet die Auflösung (`# resolved 49/49 overthinking markers`).

### 2. Server starten

Beispielaufruf (aus dem Repo-Root; loggt nach `/tmp/opencode/server.log`):

```bash
setsid nohup env MODEL=./Qwen3.8-27B-Escha-W2 VENV=.venv \
    MEM=0.92 CTXLEN=100000 HOST=0.0.0.0 API_KEY=marec \
    CUDA_GRAPH_BS='1 2 4' EXTRA_ARGS="--enable-metrics" \
    bash runtime/sglang/serve.sh >/tmp/opencode/server.log 2>&1 &
```

Die wichtigsten `serve.sh`-Env-Vars (vollständige Liste im Skriptkopf):

| Var | Bedeutung |
|---|---|
| `MODEL` | Modellordner (Pflicht) |
| `VENV` | zu aktivierendes venv (hier `.venv` im Repo) |
| `MEM` | `--mem-fraction-static` (KV-Pool-Größe) |
| `CTXLEN` | Kontext-Cap pro Request |
| `HOST` / `PORT` | Bind-Adresse (Default 127.0.0.1:30000); `API_KEY` bei `0.0.0.0` setzen! |
| `API_KEY` | Bearer-Key für Clients |
| `CUDA_GRAPH_BS` | erfasste CUDA-Graph-Batchgrößen |
| `EXTRA_ARGS` | rohe Zusatz-Flags für `launch_server` (z. B. `--enable-metrics`) |
| `ENABLE_CLP` | `1` (= Default) → `--enable-custom-logit-processor` — **Voraussetzung für Penalty UND Budget** |
| `OTP` | `1` → `--default-overthinking-penalty`: Penalty wird **serverseitig in jeden Request injiziert**, der keinen eigenen `custom_logit_processor` setzt (siehe unten) |
| `PENALTY_LAMBDA` | Default-λ des injizierten Payloads (`2.0`) — nur relevant bei `OTP=1` |
| `PENALTY_ALWAYS` | `1` → `--penalty-always` (jeder Token statt nur `<think>`); Default `0` = think-only |

### 2b. Server-Default-Penalty (`OTP=1`) — kein Request-Feld nötig

Seit dem Patch `patches/server-default-penalty.patch` (gegen die im Wheel
ausgelieferte SGLang-Fork, anwendbar mit `git apply` aus dem venv-Root; Punkte
A–D dokumentiert in `PLAN-server-default-penalty.md`) baut der Server beim
Start selbst einen Penalty-Payload — gleicher Processor, gleiche Marker, nur
ohne den CLI-Umweg:

```bash
MODEL=./Qwen3.8-27B-Escha-W2 OTP=1 bash runtime/sglang/serve.sh
# Startlog: "default overthinking penalty active (lambda=2.0, think_only=True)"
```

- **Wirkt auf**: chat/completions, completions und `/generate` — jeder Request
  ohne eigenes `custom_logit_processor`. Requests MIT eigenem Payload bleiben
  unangetastet.
- **Vorrang unverändert**: request-`custom_params` > Server-Default > Fallback.
  Ein Client kann pro Request abschalten: `"custom_params": {"penalty_lambda": 0}`,
  oder kombinieren: `{"penalty_lambda": 4.0, "thinking_budget": 28672}`.
- `OTP=1` ohne `ENABLE_CLP=1` bricht in `serve.sh` mit Fehler ab (der injizierte
  Payload wäre sonst ein Custom-Logit-Processor auf einem Server, der die
  ablehnt).
- Deaktiviert = Default: ohne `OTP=1` verhält sich der Server bit-identisch zum
  ungepatchten Stand.

Laufendes Beispiel (alle vier Varianten gegen einen `OTP=1`-Server):

```bash
.venv/bin/python runtime/sglang/examples/penalty_example.py [BASE_URL]
```

### 3. Requests senden

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

Rezepte:

| Ziel | `custom_params` |
|---|---|
| Default (λ=2, nur im Denken) | `{}` weglassen oder `{}` |
| Stärker / paper-treu überall | `{"penalty_lambda": 4.0, "think_only": false}` |
| **Komplett abschalten** (pro Request) | `{"penalty_lambda": 0}` |
| Nur Deckel auf dem Denken | `{"penalty_lambda": 0, "thinking_budget": 28672}` |
| Beides kombiniert | `{"penalty_lambda": 2.0, "think_only": true, "thinking_budget": 28672}` |

### 4. Wirkung prüfen (`--demo`)

```bash
.venv/bin/python runtime/sglang/overthinking_penalty.py \
    --model ./Qwen3.8-27B-Escha-W2 --demo
```

Vergleicht für einen Reasoning-Prompt drei Konfigurationen (off / default / λ=4) und
reportet `finish_reason`, `completion_tokens`, `reasoning_chars`, `answer_chars`.
Erwartbarer Effekt: weniger Reasoning-Zeichen, öfter `stop` statt `length`.

---

## Verifikation (durchgeführt)

- `py_compile` sauber; Payload ist valides JSON, dill-Roundtrip lädt die Klasse
  (49 Tokens, Think-IDs 248068/248069, Defaults korrekt eingebacken).
- Logiktests mit Fake-Reqs/Tensoren: inside-think bestraft, closed-think/nicht-think
  unberührt (think_only), `λ=0` aus, `{}`-Params → Defaults, `think_only:false` →
  always-on, Request-λ-Override (z. B. 4.5), Garbage-λ ohne Crash, leere/None-Paramliste
  unverändert.
- Budget-Tests: Budget überschritten + letztes Token ≠ Newline → Newline erzwungen;
  letztes Token = Newline → `</think>` erzwungen; unter Budget → unangetastet;
  gemischter Batch (Zeile A Maskierung, Zeile B Penalty) korrekt getrennt.
- Token-Auflösung am echten Tokenizer: 49/49, ` recheck` sauber übersprungen.

## Hinweise / Grenzen

- Die Felder `custom_logit_processor`/`custom_params` sind **SGLang-Erweiterungen** —
  Standard-OpenAI-Clients ignorieren sie; LM Studio/opencode etc. nutzen sie nicht
  automatisch (eigene Requests nötig).
- dill serialisiert die Klasse by-value; der Server muss denselben SGLang-Fork
  (gleiches Wheel) fahren wie der Payload-Builder — hier gegeben, da beide `.venv`
  bzw. dieses Wheel nutzen.
- Die Marker wirken nur als **Einzeltokens**: deshalb fehlt `" recheck"` (2 Tokens).
- Negative `penalty_lambda` wäre technisch möglich (Boost) und entspricht der
  Negativ-Kontrollbedingung des Papers — nicht empfohlen für Produktion.
