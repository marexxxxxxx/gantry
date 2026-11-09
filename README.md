<p align="right"><a href="README.de.md">🇩🇪 Deutsch</a> | <b>🇬🇧 English</b></p>

# smal_qwen — Escha runtime for Qwen3.8-27B-Escha-W2

This repository runs the 2-bit-quantized reasoning model
[`EschaLabs/Qwen3.8-27B-Escha-W2`](https://huggingface.co/EschaLabs/Qwen3.8-27B-Escha-W2)
(~10 GB of weights) on a single consumer GPU via the `escha` SGLang fork —
and adds a **training-free overthinking penalty** that stops quantized reasoning
models from talking themselves out of answers they have already found.

**Fastest path to a working server with the penalty enabled:**

```bash
# 0) once: create the venv & install — full guide: runtime/sglang/INSTALL.md
python3.12 -m venv .venv && source .venv/bin/activate
pip install "torch==2.9.*" --index-url https://download.pytorch.org/whl/cu128
pip install ./runtime/sglang/escha-*.whl

# 1) build the penalty payload once
python runtime/sglang/overthinking_penalty.py --model ./Qwen3.8-27B-Escha-W2 > otp.json

# 2) start the server with the penalty as server default
MODEL=./Qwen3.8-27B-Escha-W2 OTP=1 bash runtime/sglang/serve.sh
```

Details, GPU profiles and troubleshooting: [`runtime/sglang/INSTALL.md`](runtime/sglang/INSTALL.md).

**Switch the backend (Qwen ↔ Flint ↔ GPT-OSS ↔ Abliterated):**

```bash
bash server.sh start flint   # Flint (Ollama, 2 parallel slots × 32k) on 30001
bash server.sh switch gptoss # GPT-OSS 20B (Ollama, 64k, 1 slot) on 30001
bash server.sh switch abl    # Qwen3-14B Abliterated (Ollama, 40k, 1 slot) on 30001
bash server.sh switch qwen   # back to Qwen/SGLang
bash server.sh status        # shows all backends
```

All backends listen on the same upstream port `127.0.0.1:30001`, so the
gateway on `:30000` and all clients keep working unchanged. The switch stops
the running backend, waits for the port to be released and starts the other
one. Flint runs as a separate `ollama serve` process
(`runtime/flint/start-flint.sh`) on the system model store and unloads the
system Ollama's models on `:11434` when it starts, freeing the GPU. Configure it
with `FLINT_CTX` (default 32768), `FLINT_PARALLEL` (default 2) and
`FLINT_KEEP_ALIVE` (default -1). GPT-OSS works the same way
(`runtime/gptoss/start-gptoss.sh`) with `GPTOSS_CTX` (default 65536),
`GPTOSS_PARALLEL` (default 1) and `GPTOSS_KEEP_ALIVE` (default -1).
Abliterated is Qwen3-14B with the refusal direction removed (Q4_K_M, ~9 GB)
and runs via `runtime/abl/start-abl.sh` with `ABL_CTX` (default 40960),
`ABL_PARALLEL` (default 1), `ABL_KEEP_ALIVE` (default -1) and
`ABL_KV_CACHE_TYPE` (default q8_0 so 40k fits in 16 GB VRAM); download it once
with `bash runtime/abl/download-model.sh`. The same switch is in the
dashboard's **Server** tab.

Clients can keep their model name: while Flint, GPT-OSS or Abliterated runs,
the gateway maps the Qwen alias, the Flint model name, the GPT-OSS model name
and the Abliterated model name (`model_alias`, `flint_model`, `gptoss_model`,
`abl_model`) to the active backend's model, so the dashboard switch is
authoritative. For GPT-OSS it also sets `temperature: 0` and
`reasoning_effort: "low"` unless the client specifies them. All Ollama
backends show live values in the dashboard (requests, tok/s, context, VRAM)
even without SGLang `/metrics`.

**Embeddings on the CPU:**

```bash
bash runtime/embed/start-embed.sh   # qwen3-embedding:0.6b on 127.0.0.1:30002
bash runtime/embed/stop-embed.sh
```

The gateway routes `POST /v1/embeddings` to this sidecar, so clients keep using
`:30000` and do not need a second endpoint. It runs entirely on the CPU (no
VRAM) and therefore works alongside Qwen, Flint or GPT-OSS. The model name is
optional: the gateway maps the Qwen alias and both Ollama model names to
`qwen3-embedding:0.6b`; unknown names pass through and get Ollama's
"model not found". `run.sh` starts and stops the sidecar together with the
gateway. Tuning: `EMBED_CTX` (default 8192), `EMBED_PARALLEL` (default 1),
`EMBED_KEEP_ALIVE` (default -1), `EMBED_MODEL`, `EMBED_HOST`, `EMBED_MODELS`.

**Audio on the CPU (Whisper + Piper):**

Speech-to-text and text-to-speech run in an independent CPU sidecar on
`127.0.0.1:30003`. Clients keep using the gateway on `:30000`; no second
endpoint and no VRAM are needed.

| Whisper model | Size | Notes |
|---|---|---|
| `whisper-small` | 484 MB | default, fastest |
| `whisper-medium` | 1528 MB | better accuracy, slower |
| `whisper-large-v3-turbo` | 1618 MB | best accuracy, slowest on CPU |

| Piper voice | Alias | Rate |
|---|---|---|
| `de_DE-thorsten-medium` | `thorsten` | 22.05 kHz, male, default |
| `de_DE-kerstin-low` | `kerstin` | 22.05 kHz, female |
| `de_DE-eva_k-x_low` | `eva` | 16 kHz, female |

```bash
bash runtime/audio/setup.sh                      # dependencies + ffmpeg check
bash runtime/audio/download-models.sh --all      # ~3.8 GB to /mnt/models/audio
bash aiserver-monitor/run.sh restart             # gateway + embedding + audio
```

```bash
# Speech to text (language defaults to de)
curl http://127.0.0.1:30000/v1/audio/transcriptions \
  -F file=@satz.wav -F model=whisper-small

# Text to speech, streamed as WAV
curl http://127.0.0.1:30000/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"input": "Guten Tag.", "voice": "thorsten", "response_format": "wav"}' \
  -o antwort.wav
```

TTS supports `pcm`, `wav`, `mp3` and `opus` and streams sentence by sentence.
Limits: 100 MB upload, 60 minutes audio, 5000 characters of TTS text and a
queue of 8. Errors: `503` with a download command for a missing model, `404`
for an unknown model/voice, `429` when the queue is full, `502` when the
sidecar is down and `504` on timeout. Audio and transcripts are never logged.
The default language is standard German; Austrian and Swiss accents are
usually recognised, dialects are not guaranteed, and there are no AT/CH Piper
voices. Direct control: `runtime/audio/start-audio.sh`,
`runtime/audio/stop-audio.sh`, `runtime/audio/selftest.sh`. Tuning:
`AUDIO_MODELS_DIR`, `AUDIO_THREADS`, `AUDIO_PRELOAD`, `AUDIO_MODEL_CACHE`,
`AUDIO_MAX_QUEUE`, `AUDIO_STT_TIMEOUT`, `AUDIO_TTS_TIMEOUT`,
`AUDIO_MAX_UPLOAD_MB`, `AUDIO_HOST`, `AUDIO_PORT`. For GPU TTS,
`Audio8-Realistic-Air` (0.6B, 23 languages, 44.1 kHz) is documented below.

## Table of contents

1. [Repository layout](#1-repository-layout)
2. [Documentation map](#2-documentation-map)
3. [The overthinking penalty in one paragraph](#3-the-overthinking-penalty-in-one-paragraph)
4. [Credits](#4-credits)

---

## 1. Repository layout

| Path | Content |
|---|---|
| [`runtime/sglang/`](runtime/sglang/) | **The actual runtime**: escha wheel + sglang fork, `serve.sh` launcher, payload builders (`overthinking_penalty.py`, `thinking_budget.py`), benchmark wrapper, server-default patch, all documentation |
| [`Qwen3.8-27B-Escha-W2/`](Qwen3.8-27B-Escha-W2/) | Downloaded model weights (separate HF repo, not part of this codebase) |
| [`docs/`](docs/) | Technical deep dives: FP8 KV-cache quantization |
| [`slotmon/`](slotmon/) | **GPU slot monitor**: reads the server log passively and shows which job holds the slot (`monitor.sh start` → `http://0.0.0.0:8090`) |
| `ctxmon.sh` | Live server monitor: tok/s, current context usage, context peak |
| `server.sh` | Start/stop/status of the model server (`bash server.sh start\|stop\|status`); the dashboard keeps running |

## 2. Documentation map

All documentation exists in English and German; every page has a flag switcher at
the top.

| Topic | English | Deutsch |
|---|---|---|
| Runtime overview & how it works | [runtime/sglang/README.md](runtime/sglang/README.md) | [runtime/sglang/README.de.md](runtime/sglang/README.de.md) |
| Installation, GPU profiles, troubleshooting | [runtime/sglang/INSTALL.md](runtime/sglang/INSTALL.md) | [runtime/sglang/INSTALL.de.md](runtime/sglang/INSTALL.de.md) |
| Penalty usage guide (both activation paths, recipes, benchmarks) | [runtime/sglang/USAGE-penalty.md](runtime/sglang/USAGE-penalty.md) | [runtime/sglang/USAGE-penalty.de.md](runtime/sglang/USAGE-penalty.de.md) |
| Design doc: server-default patch (`OTP=1`) | [runtime/sglang/DESIGN-server-default-penalty.md](runtime/sglang/DESIGN-server-default-penalty.md) | [runtime/sglang/DESIGN-server-default-penalty.de.md](runtime/sglang/DESIGN-server-default-penalty.de.md) |
| FP8 KV cache: 2× context, quality A/B, HiCache verdict | [docs/kv-cache-quantization.md](docs/kv-cache-quantization.md) | [docs/kv-cache-quantisierung.md](docs/kv-cache-quantisierung.md) |

## 3. The overthinking penalty in one paragraph

Post-training quantization damages not primarily answer quality but the *termination
behavior* of the chain of thought: quantized reasoning models sample hesitation markers
("Wait", "But", "Alternatively") long after finding the correct answer, open new branches
and burn their token budget ([arXiv:2606.00206](https://arxiv.org/abs/2606.00206), Meta FAIR).
This repo implements the paper's fix as a custom logit processor: at every decoding step,
λ is subtracted from the logits of the 49 marker tokens — no training, no extra forward pass.
Enable it server-side for all requests with `OTP=1`, or per request via
`custom_logit_processor` payloads; per-request overrides always win.

## 4. Credits

- Model, quantization method, wheel and original launcher:
  [EschaLabs](https://huggingface.co/EschaLabs).
- Overthinking-penalty method: Lotfi, Kirichenko, Li, Liu (Meta FAIR),
  *"Quantized Reasoning Models Think They Need to Think Longer, but They Do Not"*
  ([arXiv:2606.00206](https://arxiv.org/abs/2606.00206)).
- The adaptations in this repo (payload builder, server-default patch, docs) are local work;
  errors are on us.

### Chatterbox (optional, CPU)

Chatterbox Multilingual V3 can be used as an additional TTS model. It runs in
its own CPU worker on `127.0.0.1:30004` with a separate virtualenv (torch 2.6,
numpy < 2), because its dependencies differ from Piper/Whisper. Clients still
use the gateway on `:30000`; Piper remains the default.

```bash
bash aiserver-monitor/run.sh chatterbox-setup   # create runtime/chatterbox/venv
bash aiserver-monitor/run.sh chatterbox-models  # ~3 GB to /mnt/models/audio/chatterbox
bash aiserver-monitor/run.sh restart            # gateway + all CPU sidecars

curl http://127.0.0.1:30000/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model": "chatterbox-v3", "voice": "default", "input": "Guten Tag.", "response_format": "wav"}' \
  -o antwort.wav
```

Model aliases are `chatterbox` and `chatterbox-v3`; the only voice is
`default` (fixed voice from the checkpoint). All audio formats work as before.
On eight CPU threads Chatterbox is roughly 2.6x slower than realtime and the
first request loads the model in about 8 seconds. A stopped worker returns
`502`, a timeout `504`.

### Audio8 Realistic Air (optional, GPU)

[`Edge0/Audio8-TTS-Realistic-Air`](https://huggingface.co/Edge0/Audio8-TTS-Realistic-Air)
is an additional TTS model: 0.6B parameters, 23 languages and a 44.1 kHz
codec. It runs in its own GPU worker on `127.0.0.1:30006`. The default voice
`de_male` is a fixed, natural-sounding male reference voice; replace it with
your own recording by pointing `AUDIO8_REFERENCE_AUDIO` and
`AUDIO8_REFERENCE_TEXT` at a WAV file and its exact transcript.

Audio8 needs the GPU, so unload the chat backend first (it occupies about
15 GB of VRAM). A pipeline step looks like this:

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

Setup, model download and reference voice:

```bash
bash runtime/audio8/setup.sh
bash runtime/audio8/download-models.sh
bash runtime/audio8/make-reference.sh   # creates the default de_male voice
```

Or through `run.sh`: `bash aiserver-monitor/run.sh audio8-setup`,
`audio8-models`, `start-audio8`, `stop-audio8` and `status-audio8`. The normal
`start` command deliberately does not start the Audio8 worker. Model aliases
are `audio8`, `audio8-realistic-air`, `audio8-tts-realistic-air` and
`audio8-tts`. Raw `pcm` output is resampled to the usual 24 kHz; `wav`, `mp3`
and `opus` keep the native 44.1 kHz. A missing model or reference returns
`503`, a running chat backend `409`, an unreachable worker `502` and a timeout
`504`. `/v1/unload` stops the worker so the CUDA context is released.
