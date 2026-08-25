<p align="right"><b>🇩🇪 Deutsch</b> | <a href="INSTALL.md">🇬🇧 English</a></p>

# Escha Runtime — Installation & Betrieb

Dieses Repository ist die **Laufzeitumgebung**: ein Wheel (`escha-*.whl`) mit dem
kompilierten `escha`-Paket + einem gesäuberten `sglang`-Fork, dazu der
`serve.sh`-Launcher. Es ist **modellagnostisch** — zeigen Sie es auf einen
beliebigen Escha-Modellordner einer unterstützten Architektur. Die quantisierten
Gewichte sind ein **separates** Repository:
**`EschaLabs/Qwen3.8-27B-Escha-W2`**
(<https://huggingface.co/EschaLabs/Qwen3.8-27B-Escha-W2>).

## Inhaltsverzeichnis

1. [Voraussetzungen](#1-voraussetzungen)
2. [Installation Schritt für Schritt](#2-installation-schritt-für-schritt)
3. [Modell servieren](#3-modell-servieren)
4. [Verhalten, das man kennen sollte](#4-verhalten-das-man-kennen-sollte)
5. [Betrieb auf Ihrer GPU](#5-betrieb-auf-ihrer-gpu) — [nach Compute Capability](#nach-compute-capability-architektur) · [nach VRAM](#nach-vram) · [vier wichtige Einstellungen](#vier-einstellungen-die-sich-lohnen) · [Parallelität](#parallelität)
6. [Begrenztes Denken (`thinking_budget`)](#6-begrenztes-denken-thinking_budget)
7. [Troubleshooting](#7-troubleshooting)

---

## 1. Voraussetzungen

**Alle sind hart, und drei von ihnen schlagen still fehl:**

- **Linux x86-64 mit glibc ≥ 2.28.** Das umfasst RHEL / Rocky / Alma **8** und neuer,
  Ubuntu 20.04+ und Debian 10+. Das Wheel ist mit `manylinux_2_28_x86_64` getaggt;
  pip lehnt es auf älteren Distributionen also von vornherein ab, statt später bei
  `import escha` zu scheitern.
- **Python 3.12.** Das Wheel ist als `cp312` gebaut; jeder andere Interpreter weist es
  bei der Installation mit „not a supported wheel on this platform" zurück. (Ein Wheel
  mit kompilierten Extensions ist an einen CPython-Minor gebunden — einen 3.11/3.13-Build
  gibt es bewusst nicht.)
- **NVIDIA-GPU, Compute Capability 8.0–12.0.** Der Build ist eine Fat Binary mit SASS
  für Ampere (`sm_80`/`sm_86`), Ada (`sm_89`, z. B. RTX 4090 / L40S), Hopper (`sm_90`)
  und Blackwell (`sm_100`/`sm_120`), plus PTX für Vorwärtskompatibilität per JIT auf
  neuere GPUs. Ältere Karten (`< sm_80`, z. B. Turing) werden nicht unterstützt.
  Prüfen Sie, ob das installierte Wheel Ihre Karte tatsächlich abdeckt:
  `cuobjdump --list-elf $(python -c "import escha,os;print(os.path.dirname(escha.__file__))")/_C*.so | grep -oE 'sm_[0-9]+' | sort -u`
  — alle sechs sollten erscheinen. Steht dort nur einer, ist es ein Slim-Build; er
  schlägt auf anderen GPUs beim ersten Launch mit „no kernel image is available for
  execution on the device" fehl (Installation und der Op-Registrierungscheck aus
  Schritt 4 laufen trotzdem durch — der Fehler kommt zur Laufzeit).
- **Ein funktionierender Host-C-Compiler und Python-Dev-Header.** Triton JIT-kompiliert
  bei der CUDA-Graph-Aufzeichnung einen `cuda_utils.c`-Shim und ruft dafür `cc` auf;
  das ist unabhängig von `ptxas` und einem CUDA-Toolkit — „nur Treiber, kein Toolkit"
  reicht also nicht. Vorabtest:
  ```bash
  echo '#include <Python.h>' | gcc -xc -I"$(python -c 'import sysconfig;print(sysconfig.get_paths()["include"])')" -c - -o /dev/null
  ```
- **CUDA 12.x + passendes PyTorch 2.9.x-für-CUDA-12.** `escha._C` ist ABI-verlinkt mit
  libtorch und `libcudart.so.12`; installiert sein muss also der CUDA-12-Build von
  2.9.x (ein anderer Torch-Minor kann `import escha` scheitern lassen). Dazu ein
  Treiber, der für CUDA 12.x neu genug ist.
- **`transformers >= 5.8`.** Ältere Releases kennen die Config-Klasse dieser
  Architektur nicht. Der Server bricht **nicht** ab — er loggt einmal
  `WARNING ... config normalize FAILED`, serviert mit un-normalisierten
  Architekturparametern und produziert dann flüssig klingenden Unsinn. Das Wheel
  **pinnt `transformers>=5.8`**, ein normales `pip install ./escha-*.whl` aktualisiert
  ein altes transformers also selbständig; Sie treffen das Problem nur, wenn Sie
  transformers *danach* downgraden. Bei Müll-Generationen zuerst die
  Transformers-Version prüfen. (Umgekehrt loggt transformers 5.x eine bedrohliche
  `WARNING ... you can try downgrading to transformers==4.57.1` wegen RoPE — das ist
  eine generische Upstream-sglang-Warnung und kann **ignoriert** werden; bitte
  **nicht** downgraden.)

## 2. Installation Schritt für Schritt

1. `python3.12 -m venv .venv && source .venv/bin/activate`
2. `pip install -U pip wheel`
3. **Zuerst PyTorch aus dem CUDA-12-Index installieren** (es muss der cu12-Build sein —
   `escha._C` ist ABI-verlinkt damit, und es ist die eine Abhängigkeit, die nicht aus
   PyPI kommen kann). **Auf 2.9.x pinnen**:
   `pip install "torch==2.9.*" --index-url https://download.pytorch.org/whl/cu128`
   Ein nacktes `torch>=2.9.0` löst zu einem neueren Minor auf, und **nichts korrigiert
   das** — das Wheel deklariert 61 Abhängigkeiten, aber *keinen* `torch`-Eintrag, und
   keine seiner transitiven Abhängigkeiten pinnt einen. Sie bleiben auf dem falschen
   Minor, und `import escha` schlägt später fehl mit
   `undefined symbol: _ZN3c10...` — genau die C++-ABI-Fläche, die PyTorch über Minors
   hinweg nicht stabil hält.
4. Runtime-Wheel installieren (liegt in diesem Repo):
   `pip install ./escha-*.whl`
   — installiert die `escha`-Runtime, den gebündelten `sglang`-Fork **und dessen
   komplette Laufzeit-Abhängigkeiten** (transformers ≥ 5.8, flashinfer, xgrammar,
   msgspec, uvloop, … — alle im Wheel gepinnt). Ein separates `sglang[srt]` ist nicht
   nötig. Den ganzen Stack gegenprüfen (alle drei sollten `True` printen):
   ```bash
   python -c "import torch, escha, sglang; print(torch.cuda.is_available(), hasattr(torch.ops.escha, 'escham_decode_gemv'), bool(sglang.__version__))"
   ```
   (Beachten Sie das `import sglang` im Check — ein reiner `torch`+`escha`-Check läuft
   auch auf einem Rechner durch, der gar nicht servieren kann.)
5. Modell herunterladen (die Gewichte sind ein **separates** Repo; ein flacher Ordner —
   safetensors, Config und Tokenizer liegen im Root, kein verschachteltes Unterverzeichnis):
   ```bash
   pip install -U "huggingface_hub[cli]"
   hf download EschaLabs/Qwen3.8-27B-Escha-W2 --local-dir ./Qwen3.8-27B-Escha-W2
   ```

## 3. Modell servieren

**Servieren** — `MODEL` auf den heruntergeladenen Modellordner zeigen lassen:

```bash
MODEL=./Qwen3.8-27B-Escha-W2 bash serve.sh
```

- **Die Kernel-Route wird zur Laufzeit automatisch je GPU gewählt** — keine Env-Var
  nötig. Die Kernels kommen als eine Fatbin mit zwei Launch-Geometrie-Routen; `escha`
  fragt die Compute Capability des Geräts ab und wählt die messtechnisch optimale:
  `blackwell` (barrier-arm, SM-adaptives Split-K) auf **sm_89 Ada, sm_90 Hopper und
  sm_100/sm_120 Blackwell**; `lovelace` (Original-Geometrie) auf **sm_80/sm_86 Ampere**.
  Beide sind bit-identisch; erzwingbar mit `ESCHA_ROUTE=lovelace|blackwell`, um auf der
  eigenen Karte A/B zu messen (Hoppers Default ist eine Schätzung — A/B lohnt sich).
  Aktive Route prüfen mit
  `python -c "import escha,torch; torch.zeros(1,device='cuda'); print(torch.ops.escha.escham_route())"`
  (`2`=blackwell, `1`=lovelace).
- Die komplette Knob-Liste steht im Kopf von `serve.sh` (`MEM`, `CTXLEN`, `GRAPHS`,
  `THINK`, `RADIX`, `INT8`, `DETERMINISTIC`, …). Die Defaults sind das Profil, auf dem
  jede publizierte Zahl für dieses Modell gemessen wurde — auf einer 24-GB-Karte.
- **OpenAI-kompatibler Endpoint auf `:30000`** (`http://127.0.0.1:30000/v1`). Die
  servierte Modell-ID ist `SERVED_NAME` aus `serve.sh` (überschreibbar mit
  `SERVED_NAME=...`); auflisten mit:
  ```bash
  curl -s http://127.0.0.1:30000/v1/models | python3 -m json.tool
  ```
- **Overthinking-Penalty als Serverdefault:** `OTP=1` setzen (plus `PENALTY_LAMBDA`,
  `PENALTY_ALWAYS`) — siehe [`README.de.md`](README.de.md#8-server-default-penalty-otp1)
  und [`USAGE-penalty.de.md`](USAGE-penalty.de.md) für alle Penalty-Knobs.

## 4. Verhalten, das man kennen sollte

- **Thinking ist pro Request.** `chat_template_kwargs: {"enable_thinking": false}` wird
  vom Default-Server (`THINK=1`) respektiert, mit oder ohne `tools`; ein **top-level
  `enable_thinking`-Feld wird ignoriert** — es muss in `chat_template_kwargs`. Mit
  `THINK=0` serviert der ganze Server ohne Thinking.
- **`reasoning_effort` ist ein zweiter Hebel, und der Default ist die teure Stufe.**
  `chat_template_kwargs: {"reasoning_effort": "xhigh"|"medium"|"low"}`, Default
  **`"xhigh"`**. Jede Stufe ist genau ein injizierter System-Satz: `xhigh` fordert das
  Modell auf, Annahmen zu validieren und Alternativen abzuwägen; `low` bittet um
  knappes Denken; **`medium` injiziert nichts** — das ist das neutrale, ungelenkte
  Modell, kein Mittelwert. Wenn `xhigh` Ihren Traffic überdenken lässt, ist `medium`
  meist der richtige erste Zug, `low` bei latenzsensitivem Volumen. Jeder andere Wert
  lässt das Template raisen — sichtbar als HTTP 400; das ist ein Tippfehler, kein
  Serverfehler. Alle drei greifen nur bei eingeschaltetem Thinking, und keine davon
  *begrenzt* etwas: ein `thinking_budget` (unten) ist die einzige harte Garantie.
- **`reasoning_content` UND `content` lesen.** Bei angeschaltetem Thinking landet das
  Reasoning in `reasoning_content` und die Antwort in `content`. Ein Client, der nur
  eines von beiden liest, sieht die halbe Antwort — der häufigste
  Integrationsfehler überhaupt.
- **Strukturierte Ausgaben sind stabil.** `serve.sh` defaultet `JSON_WS=1`
  (`--constrained-json-disable-any-whitespace`), wodurch `json_schema`-Werte
  Laufzeit für Laufzeit konsistent bleiben. `JSON_WS=0` optet aus.
- **Zu lange Prompts werden abgeschnitten statt abgewiesen.** `serve.sh` defaultet
  `TRUNCATE=1` (`--allow-auto-truncate`). Ohne das wird ein Prompt über `CTXLEN` hart
  abgelehnt, und ein Harness, der den Fehler als Antwort wertet, meldet einen
  Modelfehler. Wer lieber den Fehler sehen will: `TRUNCATE=0`.
- **`usage.reasoning_tokens`** zählt die Reasoning-Spanne auf Streaming- und
  Non-Streaming-Pfaden und ist 0 ohne aktiven Reasoning-Parser. Damit lässt sich ein
  `thinking_budget` an den realen Reasoning-Kosten dimensionieren.
- **`escha.__version__`** — `python -c "import escha; print(escha.__version__)"`.
  Wird aus den Metadaten der installierten Distribution gelesen, kann sich also nicht
  vom ausgelieferten Wheel entfernen. Gehört in Bugreports.
- **`DETERMINISTIC=1`** setzt `--enable-deterministic-inference` — **auf Consumer-
  Blackwell (RTX 50 / sm_120) nicht verfügbar**: der deterministische Attention-Kernel
  überschreitet dort das Shared-Memory-Limit von 104 KB pro Block, und der Server
  steigt beim Start aus. `serve.sh` reicht unbekannte Argumente direkt an
  `launch_server` durch — ein Flag, das es nicht modelliert, bleibt also erreichbar.

## 5. Betrieb auf Ihrer GPU

Die Gewichte sind **10,15 GB** groß. Was sich von Karte zu Karte ändert, ist der
**Compute-Capability-Knob** (ein Attention-Backend-Override auf den neuesten Karten) und
das **VRAM-Budget** (Kontextlänge, die CUDA-Graph-Batchliste und ob `lm_head` als
int8-as-stored liegen bleibt). Die Kernel-Launch-Route wird zur Laufzeit gewählt — Sie
setzen sie nie.

> **Gemessen auf drei Karten: einer 32-GB-RTX 5090 (sm_120), einer 24-GB-RTX 4090 (sm_89)
> und einer 24-GB-RTX 3090 (sm_86)**; auf der 4090 wurden auch die Thinking-on-Benchmarks
> des Modells produziert. Tokens/s je GPU stehen auf der
> [Model Card](https://huggingface.co/EschaLabs/Qwen3.8-27B-Escha-W2#performance-across-gpus).
> Die Zeilen **16 GB und 40 GB+** unten sind reine *Konfigurations*-Hinweise aus
> Modellgröße und Architekturabdeckung des Wheels, **keine Messungen** — lieber eine Lücke
> als eine nie gelaufene Zahl.

### Nach Compute Capability (Architektur)

| Arch | Compute cap | Karten | Was Sie setzen |
|---|---|---|---|
| Ampere | sm_80 / sm_86 | A100, A6000, RTX 3090 | Route wählt automatisch `lovelace`, aber **`ESCHA_ROUTE=blackwell` war auf einer RTX 3090 bei Batch 1 1,72× schneller gemessen** (23,6 → 40,7 tok/s; Parität bei Batch 2–16) — setzen, wenn nur ein Nutzer bedient wird |
| Ada | sm_89 | RTX 4090, L40S | nichts — Route wählt automatisch `blackwell` (**die validierte Karte**) |
| Blackwell (Consumer, 32 GB) | sm_120 | RTX 5090 | **`ATTN_BACKEND=triton`** — gemessen, siehe *Nach VRAM* |
| Hopper | sm_90 | H100 | nichts (Route defaultet `blackwell`; A/B mit `ESCHA_ROUTE` — Hopper ist eine Schätzung) |
| Blackwell (Datacenter) | sm_100 | B200 | nichts — Route wählt automatisch `blackwell` |
| Blackwell (Consumer) | sm_120 | RTX-50-Serie | **`ATTN_BACKEND=triton`** (das Default-Backend flashinfer asserted auf dieser Hybridarchitektur bei sm_120) |

Prüfen, ob die Fatbin die eigene Karte abdeckt und welche Route aktiv ist:
```bash
python -c "import escha,torch; torch.zeros(1,device='cuda'); print('route', torch.ops.escha.escham_route())"
# Erwartet: route 2 (blackwell) auf sm_89/90/100/120 — inkl. RTX 4090/5090; route 1 (lovelace) auf sm_80/86
# Beide Routen sind bit-identische Launch-Geometrien — Forcing ist eine Performance-, keine
# Korrektheitsentscheidung. A/B lohnt auf Ampere (1,72x bei bs=1 gemessen) und Hopper (Schätzdefault).
```

### Nach VRAM

Dieser Checkpoint ist text-only, daher ist `SGLANG_VLM_TEXT_ONLY=1` der `serve.sh`-Default.

**int8-as-stored `lm_head`.** Die Runtime kann `lm_head` int8 im VRAM halten statt es beim
Laden nach fp16 zu dequantisieren: ~1,2 GB statt ~2,4 GB, gelesen an **jedem**
Dekodierschritt — bringt also sowohl VRAM als auch Single-User-Decode-Bandbreite.
`INT8=auto` (der Default) schaltet es bei ≤ 24 GB VRAM ein (`SGLANG_INT8_AUTO_VRAM_GB`),
darüber aus; `INT8=on` / `INT8=off` erzwingen. Beachten: enger gefasst als die
Mixture-of-Experts-Runtime — bei diesem dichten Modell betrifft es nur `lm_head`;
`embed_tokens` ist ein Zeilen-*Lookup*, int8 dort würde VRAM sparen, aber kaum
Decode-Bandbreite, und bleibt deshalb auf dem Dequant-Pfad.

**CUDA Graphs zählen bei diesem Modell** (`GRAPHS=1`, der `serve.sh`-Default). Es ist eine
Hybridarchitektur mit vielen kleinen Kernels pro Token; eager Decode ist launch-gebunden.
`GRAPHS=0` nur zum Debuggen einer Capture-Fehlers.

| VRAM | Beispiel | Empfohlener Start |
|---|---|---|
| **16 GB** | RTX 5060 Ti, RTX 5080 | `ATTN_BACKEND=triton MEM=0.90 CTXLEN=16384 CUDA_GRAPH_BS="1 2 4 8" bash serve.sh` — neben 10,15 GB Gewichten bleibt ein kleiner Pool; bei Capture-OOM zuerst `CTXLEN` senken. Ungetestet. |
| **24 GB** | RTX 4090, RTX 3090, L40S, A6000 | `MODEL=... bash serve.sh` — die ausgelieferten Defaults (`MEM=0.72 CTXLEN=65536 MAMBA_RATIO=0.3 RADIX=0 GRAPHS=1 CUDA_GRAPH_BS="1 2 4 8 12 16"`). **Validiert auf RTX 4090 und RTX 3090.** Single-User-Profil: der rekurrente Pool klemmt es auf **8–9 Streams**, deshalb fallen die Capture-Einträge `12`/`16` weg. Für Durchsatz: `MEM=0.86 CTXLEN=32768 MAXREQ=32 MAXMAMBA=32 CUDA_GRAPH_BS="1 2 4 8 12 16 24 32"` (4090: 649 tok/s @ 16; 3090: 383). Auf einer 3090 für Single-User zusätzlich `ESCHA_ROUTE=blackwell`. |
| **32 GB** | RTX 5090 | `ATTN_BACKEND=triton MEM=0.85 CTXLEN=65536 MAXREQ=32 MAXMAMBA=32 CUDA_GRAPH_BS="1 2 4 8 12 16 24 32" bash serve.sh` — **gemessen**: 87,1 tok/s bs=1, 955 tok/s Peak bei 16 Streams, 28,2 GB Peak von 32,6. `INT8=on` bringt +7,2–7,4 % Decode bei bs=1 im gepaarten A/B (und +12,2 % KV-Tokens, kein freies VRAM — die gesparten ~1,2 GB wandern in den Pool). |
| **40 GB+** | A100, H100, L40S-48 | `MEM=0.85 CTXLEN=131072 bash serve.sh`; int8 auto-AUS, `CTXLEN`/Parallelität frei hoch. Ungetestet. |

### Vier Einstellungen, die sich lohnen

1. **`CUDA_GRAPH_BS` muss Ihre maximale Batchgröße enthalten — und darf 32 nicht
   überschreiten.** Eine nicht aufgezeichnete Batchgröße läuft *eager* (der Server loggt
   `cuda graph: False` bei dem `#running-req`), was rund 15 % langsamer liest. 32 ist das
   Maximum M des Decode-Kernels
   (`python -c "import torch, escha; print(torch.ops.escha.escham_decode_gemv_max_m())"`);
   darüber fällt eine Batch auf einen großen-M-Pfad für Prefill durch, und die Runtime
   verweigert die Aufzeichnung eines Graphs, der den falschen Kernel einbacken würde. Die
   Default-Liste endet bei 16, weil dort der Aggregatdurchsatz auf einer 4090 peakt —
   erweitern auf `"1 2 4 8 12 16 24 32"`, wenn wirklich > 16 gleichzeitige Streams
   bedient werden.
2. **Rekurrenter State begrenzt Parallelität bei kurzem Kontext; der KV-Pool bei langem.**
   Jeder Stream hält ~0,15 GB `ssm_state`, unabhängig von der Kontextlänge — das ist die
   Grenze bei kurzen Prompts. Bei langen bindet zuerst der KV-Pool: `max_total_num_tokens`
   (beim Start geloggt) geteilt durch typisches ISL+OSL ist die echte Stream-Obergrenze.
   **Loggt der Server ein `#running-req` unter der angeforderten Batch, ist der Rest
   Warteschlange, und Ihre TTFT enthält Wartezeit** — gemessen auf einer 3090 bei
   2048/2048: 32 angefragt, 18 gelaufen, mittlere TTFT 140 s — liest sich wie eine
   Prefill-Katastrophe und ist keine.
3. **`RADIX=0`, außer Sie haben gemessen, dass Prefix-Caching hilft.** Auf dieser
   Hybridarchitektur ist der Radix-Cache inkompatibel mit dem Overlap-Scheduler;
   `RADIX=1` *schaltet ihn still ab* — der Server loggt `Disabling overlap schedule since
   mamba no_buffer is not compatible with overlap schedule`. `RADIX=1` nur für
   Multi-Turn-Agent-Traffic, der lange Prefixes wirklich wiederverwendet.
4. **Wenn der Start OOM wirft: `MEM` RUNTER, nicht rauf.** `mem-fraction-static` bemißt
   den Pool, aus dem der KV-Cache geschnitten wird; *zu niedrig* scheitert ebenfalls —
   mit der umgekehrten Meldung („Not enough memory. Please try to increase
   --mem-fraction-static"). Die beiden Fehlerfälle sind Pool-zu-klein beim Start
   (`MEM` erhöhen) und Capture-OOM nach dem Gewichtsload (`MEM` senken oder
   `CUDA_GRAPH_BS` verkürzen).

### Parallelität

Dies ist ein **hybrides** Modell: die meisten Layer sind Linear-Attention mit einem
rekurrenten State-Pool pro Request; die maximale Parallelität bestimmt dieser Pool
(`MAXMAMBA`) — nicht der KV-Speicher. Das effektive `max_running_requests` wird geklemmt
auf `min(MAXREQ, MAXMAMBA // ratio)`, ratio ≈ 4 mit Radix-Cache, ≈ 1 mit `RADIX=0`.
Eine Startup-Log-Zeile meldet die Klemme.

## 6. Begrenztes Denken (`thinking_budget`)

**Lesen Sie das, bevor Sie irgendeinen Benchmark oder Agenten gegen einen
Thinking-ON-Server laufen lassen.** Mit Thinking kann das Modell das komplette
`max_tokens`-Budget fürs Reasoning verbrauchen und seine Antwort nie erreichen. Das
Ergebnis:

```
finish_reason: "length",  content: null      # alle Tokens gingen in reasoning_content
```

Ein Eval-Harness wertet das als **falsch**, nicht als langsam, und ein Agent-Framework
sieht einen No-op. Die meisten Harnesses deckeln `max_tokens` bei 512–4k — genau da
beißt es. Wie lange das Modell denkt, schwankt Laufzeit für Laufzeit; ein Deckel, der
auf einem Prompt funktioniert, kann am nächsten scheitern — man will eine *Garantie*,
keine größere Zahl. `reasoning_effort: "low"` *bittet* um weniger; es begrenzt nichts.

Ein **Thinking-Budget** ist diese Garantie: nach N Reasoning-Tokens erzwingt der Server
`</think>`, und das Modell muss innerhalb des Restbudgets antworten. `serve.sh` aktiviert
den Processor per Default (`ENABLE_CLP=1`); das Budget geht pro Request:

```bash
MODEL=/path/to/model bash serve.sh                                 # ENABLE_CLP=1 ist Default
python thinking_budget.py --model /path/to/model > clp.json        # einmaliger Payload
```

```jsonc
{ "messages": [ /* ... */ ], "max_tokens": 32768,
  "chat_template_kwargs": {"enable_thinking": true, "reasoning_effort": "xhigh"},
  "custom_logit_processor": "<Inhalt von clp.json>",   // SGLang-Erweiterung
  "custom_params": {"thinking_budget": 28672} }
```

Zwischen `thinking_budget` und `max_tokens` echtes Kopfspiel lassen — mindestens ~512
Tokens, bei Code-Antworten deutlich mehr. **Jede publizierte Thinking-on-Benchmark-Zahl
für dieses Modell (GPQA-Diamond, LiveCodeBench) wurde mit `thinking_budget` 28 672 in
`max_tokens` 32 768 produziert** (es blieben also immer 4 096 Tokens für die Antwort), bei
`reasoning_effort: "xhigh"`. Die Commonsense-Suite lief mit Thinking-off und nutzt nichts
davon.

`thinking_budget.py` liest `<think>` / `</think>` aus dem Tokenizer Ihres eigenen Modells;
es funktioniert daher mit jedem Escha-Checkpoint, der einen Thinking-Modus hat. (SGLangs
eingebauter `Qwen3ThinkingBudgetLogitProcessor` hardcodet Qwen3s Token-IDs — falsch für
das ~248k-Vokabular dieses Modells; daher der Helper.) Gegenprüfen gegen einen laufenden
Server mit `python thinking_budget.py --model <dir> --demo`.

Wer gar nicht denken will: `THINK=0` patcht das Chat-Template des Modells auf
Default-geschlossen — und unterdrückt auch die Reasoning-Effort-Anweisung, die das
Template sonst in den System-Prompt injiziert — mit gewissen Einbußen bei
reasoning-abhängiger Genauigkeit.

## 7. Troubleshooting

- **`subprocess.CalledProcessError` von `gcc` während „Capture cuda graph".** Kein
  escha-/sglang-Fehler — Ihr Host-C-Compiler baut tritons Shim nicht, und die sichtbaren
  Frames des Tracebacks (`cuda_graph_runner.py`, `triton_backend.py`) zeigen alle woanders
  hin. Den Compiler-Vorabtest aus *Voraussetzungen* ausführen. Auf schlanken
  Container-Images bricht ein in dpkg registriertes, aber auf der Platte fehlendes
  `libisl` `cc1`, während `gcc --version` trotzdem funktioniert.

- **`finish_reason: "length"` mit `content: null` auf einem Thinking-ON-Server.** Das
  Reasoning ist über Ihr `max_tokens` hinausgelaufen. Deckel erhöhen oder besser ein
  [Thinking-Budget](#6-begrenztes-denken-thinking_budget) setzen, damit immer eine
  Antwort entsteht.

- **HTTP 400 mit „Unexpected reasoning effort".** `reasoning_effort` muss exakt `xhigh`,
  `medium` oder `low` sein. Das Chat-Template raiset bei allem anderen.

- **„... has no SGLang implementation and the Transformers implementation is not
  compatible".** Dieser Fehler ist meist ein **Ablenkungsmanöver** — der Fork
  registriert die Architektur sehr wohl; die wahre Ursache ist eine Exception beim
  *Importieren* des Modellmoduls (typisch: ein zu neues `nvidia-cutlass-dsl`, das das
  Wheel auf `<4.4.0` pinnt). Den wahren Traceback holen mit:
  ```bash
  python -c "import sglang.srt.models.qwen3_5"
  ```

- **`ValueError: Invalid quantization method ...`.** Die Registry im Wheel enthält genau
  zwei Methoden: `escha` (dense) und `eschamoe` (mixture-of-experts). Prüfen, dass
  `quantize_config.json` / `config.json` Ihres Modells eine davon nennt, und dass Sie
  DIESES Wheel installiert haben (`python -c "import escha; print(escha.__version__)"`
  muss `1.2.0+qwen3dense` printen) — die 1.0.x-Wheels registrierten nur `eschamoe` und
  können ein denses Modell gar nicht laden.

- **`ModuleNotFoundError: pybase64` / `msgspec` / `xgrammar` / `flashinfer` beim Start.**
  Das Wheel deklariert die komplette Laufzeit-Abhängigkeit; ein sauberes
  `pip install ./escha-*.whl` zieht sie automatisch. Wenn Sie das sehen, wurde das Wheel
  wohl mit `--no-deps` oder in ein venv ohne auflösbare Abhängigkeiten installiert — ohne
  `--no-deps` neu installieren.

- **`ImportError: undefined symbol: _ZN3c10...` bei `import escha`.** Falsches Torch. Das
  Wheel pinnt es nicht, eine unpinnte Installation bleibt also auf einem neueren Minor.
  Prüfen mit `python -c "import torch;print(torch.__version__)"` — es muss
  `2.9.x+cu12` sein. Fix:
  `pip install "torch==2.9.*" --index-url https://download.pytorch.org/whl/cu128`.

- **`PermissionError` in einem CUDA-`bin`-Verzeichnis beim Start.** `TRITON_PTXAS_PATH`
  muss auf die ptxas-*Binärdatei* zeigen, nicht auf das Verzeichnis darüber —
  `[[ -x <dir> ]]` gilt auch für Verzeichnisse, sodass ein Shell-Profil, das das
  Verzeichnis setzt, einen naiven Check passiert und dann in triton scheitert.
  `serve.sh` erkennt und repariert diesen Fall; wer `launch_server` direkt startet,
  korrigiert die Variable selbst (oder unsetted sie einfach — triton bringt sein eigenes
  ptxas mit).

- **Ein lokaler Benchmark-Client meldet HTTP 504 auf `127.0.0.1`.** Prüfen, ob ein
  `HTTP_PROXY` / `ALL_PROXY` ohne `no_proxy=127.0.0.1` gesetzt ist. Proxies brechen
  minutenlange Requests ab und synthetisieren einen 504, während der Server längst
  weiterarbeitet.

- **OOM beim Start mit einer PID, die nicht Ihre ist.** Ein früherer
  `sglang::scheduler`-Child hält die GPU noch — das Töten des `launch_server`-Parents
  räumt ihn nicht weg. Prüfen mit
  `nvidia-smi --query-compute-apps=pid,used_memory --format=csv` und die PID killen.
  (Auf den Prozess*namen* matchen; ein `pkill -f sglang`-Pattern trifft auch die Shell,
  die ihn ausführt.)

- **Müll-Generationen / flüssig klingender Unsinn.** Zuerst `transformers` prüfen
  (≥ 5.8); die transformers-5.x-Warnung „downgrade to 4.57.1" wegen RoPE ist harmlos —
  nicht danach handeln.

- **Zwei Greedy-Requests liefern verschiedenen Text.** Erwartet: Greedy ist nicht
  bit-reproduzierbar über Requests hinweg, weil die fp16-Akkumulationsreihenfolge von
  der Batchzusammensetzung abhängt und eine lange Reasoning-Kette auseinanderläuft, sobald
  sich ein Near-Tie kippt. Für Reproduzierbarkeit `DETERMINISTIC=1`.

- **Strukturierte Ausgaben (`response_format`/JSON-Schema) auf einer knappen Karte.** Der
  Grammatik-Pfad braucht beim ersten Lauf ~0,5 GB Extra-Kopfplay (xgrammar + Sampler-
  Buchhaltung). Wer die Kante packt, plant das ein — notfalls `CTXLEN` eine Stufe senken,
  wenn der erste strukturierte Request knapp wird.



