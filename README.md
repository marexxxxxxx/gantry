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
