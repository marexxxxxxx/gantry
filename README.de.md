<p align="right"><b>🇩🇪 Deutsch</b> | <a href="README.md">🇬🇧 English</a></p>

# smal_qwen — Escha-Runtime für Qwen3.8-27B-Escha-W2

Dieses Repository betreibt das 2-Bit-quantisierte Reasoning-Modell
[`EschaLabs/Qwen3.8-27B-Escha-W2`](https://huggingface.co/EschaLabs/Qwen3.8-27B-Escha-W2)
(~10 GB Gewichte) auf einer einzelnen Consumer-GPU via dem `escha`-SGLang-Fork —
und ergänzt eine **training-freie Overthinking-Penalty**, die quantisierte
Reasoning-Modelle daran hindert, sich aus bereits gefundenen Antworten
hinauszureden.

**Schnellster Weg zu einem laufenden Server mit aktivierter Penalty:**

```bash
# 0) einmalig: venv anlegen & installieren — vollständige Anleitung: runtime/sglang/INSTALL.de.md
python3.12 -m venv .venv && source .venv/bin/activate
pip install "torch==2.9.*" --index-url https://download.pytorch.org/whl/cu128
pip install ./runtime/sglang/escha-*.whl

# 1) Penalty-Payload einmalig bauen
python runtime/sglang/overthinking_penalty.py --model ./Qwen3.8-27B-Escha-W2 > otp.json

# 2) Server mit Penalty als Serverdefault starten
MODEL=./Qwen3.8-27B-Escha-W2 OTP=1 bash runtime/sglang/serve.sh
```

Details, GPU-Profile und Troubleshooting: [`runtime/sglang/INSTALL.de.md`](runtime/sglang/INSTALL.de.md).

**Backend wechseln (Qwen ↔ Flint ↔ GPT-OSS ↔ Abliterated):**

```bash
bash server.sh start flint   # Flint (Ollama, 2 parallele Slots à 32k) auf 30001
bash server.sh switch gptoss # GPT-OSS 20B (Ollama, 64k, 1 Slot) auf 30001
bash server.sh switch abl    # Qwen3-14B Abliterated (Ollama, 40k, 1 Slot) auf 30001
bash server.sh switch qwen   # zurueck auf Qwen/SGLang
bash server.sh status        # zeigt alle Backends
```

Alle Backends lauschen auf demselben Upstream-Port `127.0.0.1:30001`; das
Gateway auf `:30000` und alle Clients bleiben unveraendert. Der Wechsel stoppt
das laufende Backend, wartet auf die Portfreigabe und startet das andere.
Flint laeuft als eigener `ollama serve`-Prozess (`runtime/flint/start-flint.sh`)
auf dem System-Modelstore und entlaedt beim Start die Modelle des
System-Ollama auf `:11434`, damit die GPU frei wird. Konfigurierbar per
`FLINT_CTX` (Default 32768), `FLINT_PARALLEL` (Default 2) und
`FLINT_KEEP_ALIVE` (Default -1). GPT-OSS laeuft genauso als eigener Prozess
(`runtime/gptoss/start-gptoss.sh`) mit `GPTOSS_CTX` (Default 65536),
`GPTOSS_PARALLEL` (Default 1) und `GPTOSS_KEEP_ALIVE` (Default -1).
Abliterated ist Qwen3-14B ohne Refusal-Verhalten (Q4_K_M, ~9 GB) und laeuft
ueber `runtime/abl/start-abl.sh` mit `ABL_CTX` (Default 40960),
`ABL_PARALLEL` (Default 1), `ABL_KEEP_ALIVE` (Default -1) und
`ABL_KV_CACHE_TYPE` (Default q8_0, damit 40k in 16 GB VRAM passen); einmalig
laden mit `bash runtime/abl/download-model.sh`. Im Dashboard gibt es denselben
Umschalter im Tab **Server**.

Clients duerfen ihren Modellnamen behalten: laeuft Flint, GPT-OSS oder
Abliterated, bildet das Gateway den Qwen-Alias, den Flint-Namen, den
GPT-OSS-Namen und den Abliterated-Namen (`model_alias`, `flint_model`,
`gptoss_model`, `abl_model`) automatisch auf das aktive Backend-Modell ab –
der Schalter im Dashboard ist also autoritativ. Fuer GPT-OSS setzt es
zusaetzlich `temperature: 0` und `reasoning_effort: "low"`
(Hindsight-Defaults), sofern der Client nichts anderes mitschickt. Das
Dashboard zeigt bei allen Ollama-Backends Live-Werte (Anfragen, tok/s,
Kontext, VRAM), auch ohne SGLang-`/metrics`.

**Embeddings auf der CPU:**

```bash
bash runtime/embed/start-embed.sh   # qwen3-embedding:0.6b auf 127.0.0.1:30002
bash runtime/embed/stop-embed.sh
```

Das Gateway routet `POST /v1/embeddings` an diesen Sidecar; Clients bleiben bei
`:30000` und brauchen keinen zweiten Endpunkt. Er laeuft komplett auf der CPU
(kein VRAM) und funktioniert deshalb parallel zu Qwen, Flint oder GPT-OSS. Der
Modellname ist optional: Das Gateway bildet den Qwen-Alias und beide
Ollama-Modellnamen auf `qwen3-embedding:0.6b` ab; unbekannte Namen laufen
unveraendert durch und erhalten Ollamas „model not found“. `run.sh` startet und
stoppt den Sidecar zusammen mit dem Gateway. Tuning: `EMBED_CTX` (Default
8192), `EMBED_PARALLEL` (Default 1), `EMBED_KEEP_ALIVE` (Default -1),
`EMBED_MODEL`, `EMBED_HOST`, `EMBED_MODELS`.

**Audio auf der CPU (Whisper + Piper):**

Spracherkennung und Sprachausgabe laufen in einem eigenen CPU-Sidecar auf
`127.0.0.1:30003`. Clients bleiben beim Gateway auf `:30000`; ein zweiter
Endpunkt und VRAM sind nicht nötig.

| Whisper-Modell | Größe | Hinweis |
|---|---|---|
| `whisper-small` | 484 MB | Default, am schnellsten |
| `whisper-medium` | 1528 MB | genauer, langsamer |
| `whisper-large-v3-turbo` | 1618 MB | beste Qualität, auf der CPU am langsamsten |

| Piper-Stimme | Alias | Rate |
|---|---|---|
| `de_DE-thorsten-medium` | `thorsten` | 22,05 kHz, männlich, Default |
| `de_DE-kerstin-low` | `kerstin` | 22,05 kHz, weiblich |
| `de_DE-eva_k-x_low` | `eva` | 16 kHz, weiblich |

```bash
bash runtime/audio/setup.sh                      # Abhängigkeiten + ffmpeg-Check
bash runtime/audio/download-models.sh --all      # ca. 3,8 GB nach /mnt/models/audio
bash aiserver-monitor/run.sh restart             # Gateway + Embedding + Audio
```

```bash
# Sprache zu Text (language ist standardmäßig de)
curl http://127.0.0.1:30000/v1/audio/transcriptions \
  -F file=@satz.wav -F model=whisper-small

# Text zu Sprache, gestreamt als WAV
curl http://127.0.0.1:30000/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"input": "Guten Tag.", "voice": "thorsten", "response_format": "wav"}' \
  -o antwort.wav
```

TTS unterstützt `pcm`, `wav`, `mp3` und `opus` und streamt Satz für Satz.
Grenzen: 100 MB Upload, 60 Minuten Audio, 5000 Zeichen TTS-Text und eine
Warteschlange von 8. Fehler: `503` mit Download-Befehl bei fehlendem Modell,
`404` bei unbekanntem Modell/Stimme, `429` bei voller Warteschlange, `502`
wenn der Sidecar aus ist und `504` bei Timeout. Audio und Transkripte werden
nie geloggt. Default ist Standarddeutsch; österreichische und Schweizer
Akzente werden meist erkannt, Mundart nicht garantiert, und es gibt keine
AT/CH-Piper-Stimmen. Direkte Steuerung: `runtime/audio/start-audio.sh`,
`runtime/audio/stop-audio.sh`, `runtime/audio/selftest.sh`. Tuning:
`AUDIO_MODELS_DIR`, `AUDIO_THREADS`, `AUDIO_PRELOAD`, `AUDIO_MODEL_CACHE`,
`AUDIO_MAX_QUEUE`, `AUDIO_STT_TIMEOUT`, `AUDIO_TTS_TIMEOUT`,
`AUDIO_MAX_UPLOAD_MB`, `AUDIO_HOST`, `AUDIO_PORT`. Für GPU-TTS ist
`Audio8-Realistic-Air` (0,6B, 23 Sprachen, 44,1 kHz) weiter unten dokumentiert.

## Inhaltsverzeichnis

1. [Repository-Aufbau](#1-repository-aufbau)
2. [Dokumentationskarte](#2-dokumentationskarte)
3. [Die Overthinking-Penalty in einem Absatz](#3-die-overthinking-penalty-in-einem-absatz)
4. [Credits](#4-credits)

---

## 1. Repository-Aufbau

| Pfad | Inhalt |
|---|---|
| [`runtime/sglang/`](runtime/sglang/) | **Die eigentliche Laufzeitumgebung**: escha-Wheel + SGLang-Fork, `serve.sh`-Launcher, Payload-Builder (`overthinking_penalty.py`, `thinking_budget.py`), Benchmark-Wrapper, Server-Default-Patch, sämtliche Dokumentation |
| [`Qwen3.8-27B-Escha-W2/`](Qwen3.8-27B-Escha-W2/) | Heruntergeladene Modellgewichte (separates HF-Repo, nicht Teil dieser Codebasis) |
| [`docs/`](docs/) | Technische Vertiefungen: FP8-KV-Cache-Quantisierung |
| [`slotmon/`](slotmon/) | **GPU-Slot-Monitor**: liest das Server-Log passiv mit und zeigt, welcher Auftrag den Slot hält (`monitor.sh start` → `http://0.0.0.0:8090`) |
| `ctxmon.sh` | Live-Monitor für den Server: tok/s, aktuelle Kontextbelegung, Kontext-Peak |
| `server.sh` | Modellserver starten/stoppen/Status (`bash server.sh start\|stop\|status`); das Dashboard läuft weiter |

## 2. Dokumentationskarte

Alle Dokumentation existiert auf Deutsch und Englisch; jede Seite hat oben einen
Flaggen-Umschalter.

| Thema | Deutsch | English |
|---|---|---|
| Runtime-Überblick & Funktionsweise | [runtime/sglang/README.de.md](runtime/sglang/README.de.md) | [runtime/sglang/README.md](runtime/sglang/README.md) |
| Installation, GPU-Profile, Troubleshooting | [runtime/sglang/INSTALL.de.md](runtime/sglang/INSTALL.de.md) | [runtime/sglang/INSTALL.md](runtime/sglang/INSTALL.md) |
| Penalty-Bedienungsanleitung (beide Aktivierungswege, Rezepte, Benchmarks) | [runtime/sglang/USAGE-penalty.de.md](runtime/sglang/USAGE-penalty.de.md) | [runtime/sglang/USAGE-penalty.md](runtime/sglang/USAGE-penalty.md) |
| Design-Doku: Server-Default-Patch (`OTP=1`) | [runtime/sglang/DESIGN-server-default-penalty.de.md](runtime/sglang/DESIGN-server-default-penalty.de.md) | [runtime/sglang/DESIGN-server-default-penalty.md](runtime/sglang/DESIGN-server-default-penalty.md) |
| FP8-KV-Cache: 2× Kontext, Qualitäts-A/B, HiCache-Urteil | [docs/kv-cache-quantisierung.md](docs/kv-cache-quantisierung.md) | [docs/kv-cache-quantization.md](docs/kv-cache-quantization.md) |

## 3. Die Overthinking-Penalty in einem Absatz

Post-Training-Quantisierung beschädigt nicht primär die Antwortqualität, sondern das
*Terminierungsverhalten* der Gedankenkette: quantisierte Reasoning-Modelle sampeln
Zögerungsmarker („Wait“, „But“, „Alternatively“) noch lange nach der bereits gefundenen
richtigen Antwort, öffnen neue Denkzweige und verbrennen ihr Token-Budget
([arXiv:2606.00206](https://arxiv.org/abs/2606.00206), Meta FAIR). Dieses Repo setzt den
Fix des Papers als Custom-Logit-Processor um: an jedem Dekodierschritt wird von den Logits
der 49 Markertokens λ abgezogen — kein Training, kein Extra-Forward-Pass. Aktivierung
serverseitig für alle Requests mit `OTP=1` oder pro Request via
`custom_logit_processor`-Payload; Request-Overrides gewinnen immer.

## 4. Credits

- Modell, Quantisierungsmethode, Wheel und Original-Launcher:
  [EschaLabs](https://huggingface.co/EschaLabs).
- Overthinking-Penalty-Methode: Lotfi, Kirichenko, Li, Liu (Meta FAIR),
  *"Quantized Reasoning Models Think They Need to Think Longer, but They Do Not"*
  ([arXiv:2606.00206](https://arxiv.org/abs/2606.00206)).
- Die Adaptionen in diesem Repo (Payload-Builder, Server-Default-Patch, Doku) sind
  lokale Arbeit; Fehler gehen auf uns.

### Chatterbox (optional, CPU)

Chatterbox Multilingual V3 kann als zusätzliches TTS-Modell genutzt werden.
Es läuft in einem eigenen CPU-Worker auf `127.0.0.1:30004` mit separater
virtueller Umgebung (torch 2.6, numpy < 2), weil die Abhängigkeiten von
Piper/Whisper abweichen. Clients nutzen weiterhin das Gateway auf `:30000`;
Piper bleibt der Standard.

```bash
bash aiserver-monitor/run.sh chatterbox-setup   # runtime/chatterbox/venv anlegen
bash aiserver-monitor/run.sh chatterbox-models  # ~3 GB nach /mnt/models/audio/chatterbox
bash aiserver-monitor/run.sh restart            # Gateway + alle CPU-Sidecars

curl http://127.0.0.1:30000/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model": "chatterbox-v3", "voice": "default", "input": "Guten Tag.", "response_format": "wav"}' \
  -o antwort.wav
```

Modell-Aliase sind `chatterbox` und `chatterbox-v3`; die einzige Stimme ist
`default` (feste Stimme aus dem Checkpoint). Alle Audioformate funktionieren
wie bisher. Auf acht CPU-Threads ist Chatterbox etwa 2,6x langsamer als
Echtzeit; die erste Anfrage lädt das Modell in etwa 8 Sekunden. Ein gestoppter
Worker liefert `502`, ein Timeout `504`.

### Audio8 Realistic Air (optional, GPU)

[`Edge0/Audio8-TTS-Realistic-Air`](https://huggingface.co/Edge0/Audio8-TTS-Realistic-Air)
ist ein weiteres TTS-Modell: 0,6B Parameter, 23 Sprachen und ein
44,1-kHz-Codec. Es läuft in einem eigenen GPU-Worker auf `127.0.0.1:30006`.
Die Default-Stimme `de_male` ist eine feste, möglichst natürliche männliche
Referenzstimme; für eine eigene Aufnahme `AUDIO8_REFERENCE_AUDIO` und
`AUDIO8_REFERENCE_TEXT` auf eine WAV-Datei und ihr exaktes Transkript zeigen.

Audio8 braucht die GPU, deshalb zuerst das Chat-Backend entladen (es belegt
etwa 15 GB VRAM). Ein Pipeline-Schritt sieht so aus:

```bash
curl -X POST http://127.0.0.1:30000/v1/unload
curl -X POST http://127.0.0.1:30000/v1/load \
  -H 'Content-Type: application/json' -d '{"model": "Audio8-Realistic-Air"}'
curl http://127.0.0.1:30000/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model": "Audio8-Realistic-Air", "input": "Guten Tag.", "voice": "de_male", "language": "German", "response_format": "wav"}' \
  -o antwort.wav
curl -X POST http://127.0.0.1:30000/v1/unload
```

Setup, Modelldownload und Referenzstimme:

```bash
bash runtime/audio8/setup.sh
bash runtime/audio8/download-models.sh
bash runtime/audio8/make-reference.sh   # legt die Default-Stimme de_male an
```

Oder über `run.sh`: `bash aiserver-monitor/run.sh audio8-setup`,
`audio8-models`, `start-audio8`, `stop-audio8` und `status-audio8`. Der normale
`start`-Befehl startet den Audio8-Worker absichtlich nicht. Modell-Aliase sind
`audio8`, `audio8-realistic-air`, `audio8-tts-realistic-air` und `audio8-tts`.
Rohes `pcm` wird wie üblich auf 24 kHz resampelt; `wav`, `mp3` und `opus`
behalten die nativen 44,1 kHz. Ein fehlendes Modell oder eine fehlende Referenz
liefert `503`, ein laufendes Chat-Backend `409`, einen nicht erreichbaren
Worker `502` und ein Timeout `504`. `/v1/unload` stoppt den Worker, damit der
CUDA-Kontext frei wird.
