<p align="right"><b>🇩🇪 Deutsch</b> | <a href="USAGE-penalty.md">🇬🇧 English</a></p>

# Overthinking-Penalty — detaillierte Benutzung

Alle Wege zum Einsatz der Overthinking-Penalty (arXiv 2606.00206) auf diesem
Qwen3.8-27B-Escha-W2-Server: vom Ein-Schalter-Serverdefault (`OTP=1`) über den
rohen CLI-Aufruf bis zur Kontrolle pro Request. Grundlagen (Marker-Mechanik,
Parametervertrag) stehen in [`README.de.md`](README.de.md); hier geht es ums
**Bedienen**.

## Inhaltsverzeichnis

0. [Die zwei Wege im Überblick](#0-die-zwei-wege-im-überblick)
1. [Schnellstart](#1-schnellstart-kopieren--laufen-lassen)
2. [Server-Default via `serve.sh`: Env-Vars](#2-server-default-via-servesh-env-vars)
3. [Roher CLI-Weg (ohne `serve.sh`)](#3-roher-cli-weg-ohne-servesh)
4. [Als Client nutzen](#4-als-client-nutzen) — [nichts tun](#41-gar-nichts-tun-der-normalfall) · [pro Request steuern](#42-pro-request-abdrehen--verstärken-custom_params) · [Batch & `/generate`](#43-batch-n1-und-natives-generate) · [Beispielskript](#44-komplett-beispiel-als-skript) · [eigener Payload](#45-eigenen-payload-statt-serverdefault-weg-a)
5. [Wirkung messen](#5-wirkung-messen)
6. [Troubleshooting / FAQ](#6-troubleshooting--faq)
7. [Flag-/Var-Referenz](#7-flag-var-referenz-kompakt)

---

## 0. Die zwei Wege im Überblick

| | Weg A: **Payload pro Request** (Original) | Weg B: **Server-Default `OTP=1`** (Patch) |
|---|---|---|
| Wer aktiviert? | der Client (schickt den Payload mit) | der Server-Admin (Startflag) |
| Client-Aufwand | Payload bauen + in jeden Request legen | **keiner** — plain OpenAI-Requests |
| Stärke/Laufzeit | im Payload gebacken (`--lambda`, `--always`) | beim Serverstart (`PENALTY_LAMBDA`, `PENALTY_ALWAYS`) |
| Pro-Request-Änderung | eigene Werte via `custom_params` | gleiche `custom_params` überschreiben die Defaults |
| Gut für | Benchmarks, unterschiedliche Profile je Request | Alltag: alle Clients profitieren ohne Umbau |

Beide Wege benutzen denselben Processor, dieselben 49 Marker, denselben
Vorrang: **request-`custom_params` > gebackene/baked-in Defaults > Fallback**.

---

## 1. Schnellstart (kopieren & laufen lassen)

```bash
cd /home/user/smal_qwen

# 1) Server MIT Penalty als Default starten (16-GB-Profil, RTX 5070 Ti):
MODEL=./Qwen3.8-27B-Escha-W2 VENV=.venv \
    MEM=0.90 CTXLEN=16384 CUDA_GRAPH_BS='1 2 4 8' \
    OTP=1 PENALTY_LAMBDA=2.0 \
    bash runtime/sglang/serve.sh

# 2) Im Startlog muss erscheinen:
#    [serve] ... OTP=1(lam=2.0)
#    default overthinking penalty active (lambda=2.0, think_only=True)

# 3) Gegen einen ZWEITEN Terminal fragen — ganz normaler Chat, KEIN Payload:
curl -s http://127.0.0.1:30000/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model": "escha-qwen38-27b-w2",
         "messages": [{"role": "user", "content": "Was ist 17*24? Denk kurz."}],
         "max_tokens": 512, "temperature": 0.6}' | head -c 600
```

Der Request oben enthält nichts Penalty-Spezifisches — der Server injiziert
den Processor selbst. Genau das ist der Sinn von `OTP=1`.

---

## 2. Server-Default via `serve.sh`: Env-Vars

`serve.sh` übersetzt die Env-Vars in die CLI-Flags von Weg B.

```bash
# Standard: an, lambda 2.0, nur innerhalb <think>
MODEL=./Qwen3.8-27B-Escha-W2 OTP=1 bash runtime/sglang/serve.sh

# Stärker und überall (auch außerhalb von <think>; paper-treu):
MODEL=./Qwen3.8-27B-Escha-W2 OTP=1 PENALTY_LAMBDA=3.0 PENALTY_ALWAYS=1 \
    bash runtime/sglang/serve.sh

# Sanfter (kaum Eingriff):
MODEL=./Qwen3.8-27B-Escha-W2 OTP=1 PENALTY_LAMBDA=1.0 \
    bash runtime/sglang/serve.sh

# Kombi mit dem Thinking-Budget-Knob bleibt unverändert möglich:
MODEL=./Qwen3.8-27B-Escha-W2 OTP=1 PENALTY_LAMBDA=2.0 INT8=on \
    MEM=0.92 CTXLEN=100000 HOST=0.0.0.0 API_KEY=marec \
    bash runtime/sglang/serve.sh
```

| Env-Var | Default | Bedeutung |
|---|---|---|
| `OTP` | `0` | `1` = Penalty als Serverdefault injizieren |
| `PENALTY_LAMBDA` | `2.0` | Stärke λ; Paper-Sweep 0.5–4.0. Höher = kürzeres/knapperes Reasoning |
| `PENALTY_ALWAYS` | `0` | `1` = jeder Token wird penalisiert, nicht nur `<think>`-Tokens (berührt auch Finalantworten/Code!) |
| `ENABLE_CLP` | `1` | Muss `1` sein, wenn `OTP=1` (sonst bricht `serve.sh` mit Fehler ab) |

Hintergrund zur Wahl von λ: 2.0 ist der Paper-Default und auf DIESEM Modell
gemessen (siehe [`README.de.md`](README.de.md), Abschnitt Demo). > 2.5 macht
Reasoning deutlich knapper; > 4.0 riskiert, dass legitime Umplanung ("wait",
"but") unterdrückt wird. Zum Ausprobieren: Server mit λ=2.0 starten und per
Request auf 4.0 hochgehen (Abschnitt 4) — ein Neustart ist dafür NICHT nötig.

---

## 3. Roher CLI-Weg (ohne `serve.sh`)

Wer den Server selbst startet (`python -m sglang.launch_server …`) oder die
Flags in ein eigenes systemd-Unit/Docker-CMD packen will, benutzt direkt:

```bash
cd /home/user/smal_qwen
source .venv/bin/activate
export LD_LIBRARY_PATH="$(python -c 'import torch,os;print(os.path.join(os.path.dirname(torch.__file__),"lib"))'):$LD_LIBRARY_PATH"
export SGLANG_MAMBA_CONV_DTYPE=float16 SGLANG_DISABLE_CUDNN_CHECK=1 \
       HF_HUB_OFFLINE=1 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
       ESCHA_STRICT=1 ESCHA_MULTI=1 ESCHA_PREFILL=fused \
       ESCHA_PREFILL_ACC=mixed ESCHA_PREFILL_FP16ACC=0 ESCHA_DENSE_FUSED_EPI=0
export TRITON_PTXAS_PATH=".venv/lib/python3.12/site-packages/triton/backends/nvidia/bin/ptxas"

python -m sglang.launch_server \
    --model-path ./Qwen3.8-27B-Escha-W2 \
    --served-model-name escha-qwen38-27b-w2 \
    --host 127.0.0.1 --port 30000 \
    --dtype float16 --mem-fraction-static 0.90 --context-length 16384 \
    --chunked-prefill-size 2048 --sampling-backend pytorch \
    --trust-remote-code --tool-call-parser qwen3_coder \
    --reasoning-parser qwen3 --enable-metrics --allow-auto-truncate \
    --attention-backend triton \
    --disable-piecewise-cuda-graph --cuda-graph-bs '1 2 4 8' \
    --kv-cache-dtype fp8_e4m3 --mamba-full-memory-ratio 0.3 \
    --constrained-json-disable-any-whitespace \
    --enable-custom-logit-processor \
    --default-overthinking-penalty \
    --penalty-lambda 2.0 \
    --penalty-think-only
```

Die letzten vier Zeilen sind der Penalty-Teil:

| Flag | Wirkung |
|---|---|
| `--enable-custom-logit-processor` | schaltet die Mechanik frei (**Pflicht** für beide Wege) |
| `--default-overthinking-penalty` | injiziert den Payload in jeden Request ohne eigenen Processor |
| `--penalty-lambda 2.0` | Default-Stärke des injizierten Payloads |
| `--penalty-think-only` \| `--penalty-always` | Scope: nur `<think>` (Default) vs. jeder Token |

Kurzformen zum Testen ohne `serve.sh`-Rest:

```bash
# minimal (nur prüfen, ob die Flags da sind):
python -m sglang.launch_server --help | grep -A1 "penalty\|custom-logit"

# hart paper-treu:
... --enable-custom-logit-processor --default-overthinking-penalty \
    --penalty-lambda 4.0 --penalty-always

# wieder AUS (Verhalten wie ungepatcht): einfach die beiden
# penalty-Flags weglassen — Default ist aus.
```

Beim Start prüft der Server den Tokenizer und loggt entweder
`default overthinking penalty active (lambda=…, think_only=True)` oder
`default overthinking penalty disabled: <Grund>` (z. B. Tokenizer ohne
`<think>`-Token). Bei `disabled` läuft der Server ganz normal WEITER — nur
ohne Default-Penalty.

---

## 4. Als Client nutzen

### 4.1 Gar nichts tun (der Normalfall)

Jeder gewöhnliche OpenAI-kompatible Aufruf bekommt die Penalty automatisch:

```bash
curl -s http://127.0.0.1:30000/v1/chat/completions \
    -H 'Content-Type: application/json' -d '{
      "model": "escha-qwen38-27b-w2",
      "messages": [{"role": "user", "content": "Denke Schritt für Schritt: …"}],
      "max_tokens": 4096, "temperature": 0.6,
      "chat_template_kwargs": {"enable_thinking": true}
    }'
```

### 4.2 Pro Request abdrehen / verstärken (`custom_params`)

`custom_params` ist eine SGLang-Erweiterung und **überschreibt** die
Serverdefaults nur für diesen Request:

```bash
# Penalty für diesen EINEN Request komplett aus:
curl -s http://127.0.0.1:30000/v1/chat/completions \
    -H 'Content-Type: application/json' -d '{
      "model": "escha-qwen38-27b-w2",
      "messages": [{"role": "user", "content": "…"}],
      "custom_params": {"penalty_lambda": 0}
    }'

# Stärker als der Serverdefault:
#   … "custom_params": {"penalty_lambda": 4.0}

# Nur den Deckel: kein Penalty, aber CoT endet nach ~28k Tokens:
#   … "custom_params": {"penalty_lambda": 0, "thinking_budget": 28672}
```

### 4.3 Batch (n>1) und natives `/generate`

Beide Pfade sind gepatcht und funktionieren unverändert:

```bash
# n=2 über chat/completions:
curl -s http://127.0.0.1:30000/v1/chat/completions \
    -H 'Content-Type: application/json' -d '{
      "model": "escha-qwen38-27b-w2",
      "messages": [{"role": "user", "content": "Zähle vorwärts."}],
      "n": 2, "max_tokens": 64, "temperature": 0
    }'

# natives /generate (sampling_params statt custom_params auf Top-Level):
curl -s http://127.0.0.1:30000/generate \
    -H 'Content-Type: application/json' -d '{
      "text": "The capital of France is",
      "sampling_params": {"max_new_tokens": 32, "temperature": 0}
    }'
```

### 4.4 Komplett-Beispiel als Skript

```bash
.venv/bin/python runtime/sglang/examples/penalty_example.py [BASE_URL]
```

Vergleicht live: Serverdefault / Override λ=0 / Override λ=4 / Kombi mit
Budget — inkl. finish_reason, Token- und Zeichen-Zahlen.

### 4.5 Eigenen Payload statt Serverdefault (Weg A)

Ein Client, der SELBST einen `custom_logit_processor` mitschickt, überschreibt
den Serverdefault für diesen Request vollständig (eigener λ, eigener Scope):

```bash
# Payload einmalig bauen:
.venv/bin/python runtime/sglang/overthinking_penalty.py \
    --model ./Qwen3.8-27B-Escha-W2 --lambda 4.0 --always > otp_always.json

# und mitschicken ("clp" = Inhalt der Datei als String):
CLP=$(cat otp_always.json); python3 - "$CLP" <<'EOF'
import json, sys, urllib.request
body = {"model": "escha-qwen38-27b-w2",
        "messages": [{"role": "user", "content": "Think step by step: 37*43"}],
        "max_tokens": 4096, "temperature": 0.6,
        "custom_logit_processor": sys.argv[1], "custom_params": {}}
r = urllib.request.Request("http://127.0.0.1:30000/v1/chat/completions",
                           json.dumps(body).encode(),
                           {"Content-Type": "application/json"})
print(json.loads(urllib.request.urlopen(r).read())["choices"][0]["message"])
EOF
```

---

## 5. Wirkung messen

1. **Log-Zeile** beim Start: `default overthinking penalty active (…)`.
2. **A/B am selben Prompt** (serverdefault an, per Request aus/an):
   `finish_reason=length` + leeres `content` = Reasoning in den Deckel gelaufen;
   mit Penalty sollte das seltener passieren und `completion_tokens` sinken.
3. **Demo-Vergleich** gegen einen laufenden Server:
   `.venv/bin/python runtime/sglang/overthinking_penalty.py --demo \
        --model ./Qwen3.8-27B-Escha-W2 --base-url http://127.0.0.1:30000/v1`
   (sendet off/default/stark und tabelliert die Ergebnisse).

---

## 6. Troubleshooting / FAQ

| Symptom | Ursache & Fix |
|---|---|
| `serve.sh` bricht ab: „OTP=1 requires ENABLE_CLP=1" | Du hast `ENABLE_CLP=0` gesetzt. Ohne CLP würde jeder Request den injizierten Payload ablehnen. Entweder weglassen (Default ist 1) oder `OTP=0`. |
| Log sagt `default overthinking penalty disabled: …` | Payload-Build fehlgeschlagen (falsche MODEL-Dir? Tokenizer ohne `<think>`?). Der Server läuft weiter, nur ohne Default-Penalty. |
| Client will wirklich KEINE Penalty, kann aber nicht umbauen | Request mit `"custom_params": {"penalty_lambda": 0}` senden — oder Server ohne `OTP=1` neu starten (dann bit-identisch zum ungepatchten Stand). |
| Antwortqualität leidet / Antworten wirken gehetzt | λ zu hoch oder `PENALTY_ALWAYS=1` (penalisiert auch Finalantworten). Runter mit `PENALTY_LAMBDA` bzw. ALWAYS ausschalten — Neustart genügt. |
| Ich sehe keinen Effekt | Prüfe die Log-Zeile (läuft der Server wirklich mit `OTP=1`?), und vergleiche A/B mit λ=0 auf DEMSELBEN Prompt. Bei kurzen Non-Thinking-Prompts (`</think>` früh geschlossen) greift think_only bewusst nicht. |
| Patch nach Wheel-Update weg | Neu anwenden: `git apply runtime/sglang/patches/server-default-penalty.patch` im venv-Root (Punkte A–D in [`DESIGN-server-default-penalty.de.md`](DESIGN-server-default-penalty.de.md)). |

---

## 7. Flag-/Var-Referenz (kompakt)

**CLI:** `--enable-custom-logit-processor` · `--default-overthinking-penalty` ·
`--penalty-lambda FLOAT` (Default 2.0) · `--penalty-think-only` (Default) /
`--penalty-always`

**serve.sh:** `OTP` · `PENALTY_LAMBDA` · `PENALTY_ALWAYS` (+ `ENABLE_CLP` als
Vorbedingung)

**Request:** `custom_params.penalty_lambda` (float, 0 = aus) ·
`custom_params.think_only` (bool) · `custom_params.thinking_budget` (int) ·
`custom_logit_processor` (String-Payload, überschreibt den Serverdefault)
