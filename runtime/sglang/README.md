<p align="right"><a href="README.de.md">🇩🇪 Deutsch</a> | <b>🇬🇧 English</b></p>

# Escha Runtime for Qwen3.8-27B-Escha-W2 — SGLang Adaptations

This directory is the **runtime environment** for the 2-bit-quantized model
[`EschaLabs/Qwen3.8-27B-Escha-W2`](https://huggingface.co/EschaLabs/Qwen3.8-27B-Escha-W2):
the `escha` wheel with the patched SGLang fork, the `serve.sh` launcher,
helper scripts for custom-logit-processor payloads (**thinking budget** and
**overthinking penalty**), and the accompanying documentation.

The centerpiece of this adaptation is a **training-free overthinking penalty**
that stops quantized reasoning models from tangling themselves up in their own
chain of thought — enabled server-side via a start flag (`OTP=1`) or controlled
per request.

## Table of contents

1. [What's in here?](#1-whats-in-here)
2. [Credits & scientific basis](#2-credits--scientific-basis)
3. [Background: overthinking under quantization](#3-background-overthinking-under-quantization)
4. [The solution: logit penalty on hesitation markers](#4-the-solution-logit-penalty-on-hesitation-markers)
5. [How it works in detail](#5-how-it-works-in-detail)
6. [Quickstart](#6-quickstart)
7. [Server launch in detail (`serve.sh`)](#7-server-launch-in-detail-servesh)
8. [Server-default penalty (`OTP=1`)](#8-server-default-penalty-otp1)
9. [Sending requests & recipes](#9-sending-requests--recipes)
10. [Measuring the effect](#10-measuring-the-effect)
11. [Verification](#11-verification)
12. [Limitations & notes](#12-limitations--notes)
13. [Further documentation](#13-further-documentation)

---

## 1. What's in here?

| File | Purpose | Status |
|---|---|---|
| `escha-1.2.0+qwen3dense-cp312-*.whl` | Runtime wheel: `escha` + SGLang fork | original |
| `serve.sh` | Server launcher; all knobs as env vars | original (see file header + [`INSTALL.md`](INSTALL.md)) |
| `start-penalty.sh` / `stop-server.sh` | Start the server in the background (penalty on) / stop it | helper scripts |
| `INSTALL.md` | Installation, GPU profiles, troubleshooting | original |
| `thinking_budget.py` | Payload builder for the thinking budget | original (helper) |
| `overthinking_penalty.py` | **Payload builder for the overthinking penalty** | **NEW — core adaptation** |
| `bench_with_penalty.py` | Benchmark wrapper: `sglang.bench_serving` with the penalty payload in every request | **NEW** |
| `DESIGN-server-default-penalty.md` | Design document for the server-default patch (code anchors, patch points A–D) | **NEW** |
| `patches/server-default-penalty.patch` | Patch against the fork: `OTP=1` injects the penalty server-side | **NEW** |
| `examples/penalty_example.py` | Example client against an `OTP=1` server: default injection, override λ=0 / λ=4, combined with budget | **NEW** |
| `USAGE-penalty.md` | **Detailed usage guide**: both activation paths, curl/Python examples, measuring the effect, troubleshooting | **NEW** |
| `README.md` / `README.de.md` | this file (EN/DE) | **NEW** |

---

## 2. Credits & scientific basis

- **Model & runtime foundation:** [`EschaLabs/Qwen3.8-27B-Escha-W2`](https://huggingface.co/EschaLabs/Qwen3.8-27B-Escha-W2)
  — the 2-bit-quantized checkpoint (escha method, ~10 GB); the `escha-1.2.0+qwen3dense`
  wheel and the original `serve.sh` launcher come from that distribution.
  All credit for quantization, kernels and benchmark profiles goes to EschaLabs.
- **Scientific basis of the overthinking penalty:**
  Lotfi, Kirichenko, Li, Liu (Meta FAIR):
  *"Quantized Reasoning Models Think They Need to Think Longer, but They Do Not"*
  ([arXiv:2606.00206](https://arxiv.org/abs/2606.00206)). The marker list, the
  penalty formula and the λ recommendations come from the paper (Appendix C,
  Table 8).
- The implementation here (`overthinking_penalty.py`, server patch) is a
  **local adaptation** for exactly this fork/checkpoint; errors in the adaptation
  are on us, not on EschaLabs or the paper authors.

---

## 3. Background: overthinking under quantization

Post-training quantization (PTQ) does not primarily damage answer quality in
reasoning models — it damages the **termination behavior of the chain of
thought**. The paper shows:

- PTQ-quantized reasoning models often find the correct answer *in the middle*
  of the chain-of-thought already …
- … but then sample **hesitation markers** ("Wait", "But", "Alternatively"),
  open new reasoning branches and talk themselves out of the answer they have
  already found.
- Consequences: CoT length up to **4.5×**, accuracy collapses (AWQ W3 on
  MATH-500: 85.6% → 47.0%).

Exactly this pattern is visible on this W2 export: reasoning traces run to the
token budget, answers end with `finish_reason: "length"` and empty
`content: null` — an eval harness scores that as **wrong**, not slow; an agent
framework sees a no-op.

---

## 4. The solution: logit penalty on hesitation markers

At **every** decoding step, a fixed value λ is subtracted from the logits of all
49 marker tokens (`z′(v) = z(v) − λ`):

- **No training, no fine-tuning, no extra forward pass** — the intervention
  happens directly on the logits of the regular decoding step; there is no
  measurable overhead.
- Paper results: CoT length **−12…−23 %**, accuracy equal or better
  (+up to 6.2 pp at AWQ W3), overthinking errors up to **−58 %**.
- The scope is configurable: by default only **inside** an open `<think>` block
  (final answers and code remain untouched); optionally paper-faithful at every
  step (`--always`).

---

## 5. How it works in detail

### 5.1 Architecture in three steps

1. **Payload build (offline, once):** `overthinking_penalty.py` resolves the
   markers against the tokens of *its own* model, defines a processor class
   with baked-in defaults, and serializes it via dill as hex-JSON to stdout —
   exactly the same transport format as `thinking_budget.py`. The server loads
   the class at runtime through the SGLang mechanism `custom_logit_processor`
   (`--enable-custom-logit-processor`; `serve.sh` sets this via `ENABLE_CLP=1`
   **by default**).
2. **Request side:** The client sends two SGLang extension fields:
   `"custom_logit_processor": "<payload>"` and optionally
   `"custom_params": {...}`. Per batch row, the fork passes a param dict to
   `processor.__call__(logits, custom_param_list)` and automatically injects
   the request object under `"__req__"` (state tracking:
   `origin_input_ids` + `output_ids`, see `schedule_batch.py`).
3. **Per batch row and decode step:** first, if applicable, the forced budget
   close-out (force newline, then `</think>`), otherwise the penalty.

### 5.2 Token research (verified against the real checkpoint)

- Resolution is **encode-based**: `tok.encode(marker, add_special_tokens=False)`
  with a round-trip check (`decode(ids) == marker`). `convert_tokens_to_ids()`
  returns only `None` for plain-text markers in this ~248k vocabulary — so the
  obvious approach just does not work here.
- **49 of 50** paper markers are single tokens with an exact round trip. Only
  `" recheck"` splits into `[311, 1960]` → skipped (with a warning on stderr).
  Examples: `" perhaps"`→8106, `" maybe"`→6970, `" wait"`→3655,
  `" Wait"`→13428, `" But"`→1921, `" alternatively"`→66073, `" wrong"`→4808.
- Thinking anchor tokens: `<think>`=248068, `</think>`=248069, newline=`198` —
  identical to the constants of the fork (`Qwen38ThinkingBudgetLogitProcessor`).
  They are read from the tokenizer at build time, never hardcoded.

### 5.3 Parameter contract (`custom_params`, all keys OPTIONAL)

| Key | Type | Meaning | Fallback |
|---|---|---|---|
| `penalty_lambda` | float | Subtraction per marker token and step. **`0` switches the penalty off completely.** | CLI `--lambda` (baked into payload), default 2.0 |
| `think_only` | bool | `true`: penalty **only inside** an open `<think>` block (final answers/code untouched). `false`: every step (paper-faithful). | CLI `--think-only`/`--always` (baked in), default `true` |
| `thinking_budget` | int | Max. reasoning tokens after `<think>`; afterwards the processor forces a newline, then `</think>`. Unset = off. | — (off) |

**Precedence chain:**

```
request custom_params  >  CLI defaults (baked into payload)  >  built-in fallbacks
```

With `OTP=1` (server default) the same chain applies — the injected payload is
just the lowest fallback; any request can override everything.

**Why penalty and budget live in one class:** SGLang allows only **one**
`custom_logit_processor` string per request. This processor therefore combines
both levers — both are usable within a single request:

```jsonc
{ "custom_logit_processor": "<otp.json>",
  "custom_params": {"penalty_lambda": 2.0, "think_only": true, "thinking_budget": 28672} }
```

**Degrade behavior (intentional):**

- If `__req__` is missing (e.g. because `custom_params` was omitted entirely),
  the think scope cannot be checked — the processor then penalizes *always*
  instead of silently skipping the row (no silent no-op).
- Invalid `penalty_lambda` (e.g. string) → the row is skipped, no crash.
- λ = 0 → penalty off; an active `thinking_budget` still applies.

### 5.4 CLI flags of `overthinking_penalty.py`

| Flag | Meaning | Default |
|---|---|---|
| `--model DIR` | Model directory (tokenizer); required | — |
| `--lambda FLOAT`, `-l` | Baked-in default λ; paper sweep 0.5–4.0 | `2.0` |
| `--think-only` / `--always` | Baked-in default scope (mutually exclusive) | `--think-only` |
| `--demo` | Test against a running server: off vs. default vs. λ=4 | off |
| `--base-url URL` | Endpoint for `--demo` | `http://127.0.0.1:30000/v1` |
| `--served-name NAME` | Model ID for `--demo` | `escha-qwen38-27b-w2` |
| `--max-tokens N` | Demo budget | `4096` |

---

## 6. Quickstart

Four steps from zero to a running server with the penalty:

```bash
cd /home/user/smal_qwen

# 1) Build the payload (once; defaults: λ=2.0, think-only)
.venv/bin/python runtime/sglang/overthinking_penalty.py \
    --model ./Qwen3.8-27B-Escha-W2 > otp.json

# 2) Start the server WITH the penalty as server default (16 GB profile):
MODEL=./Qwen3.8-27B-Escha-W2 VENV=.venv \
    MEM=0.90 CTXLEN=16384 CUDA_GRAPH_BS='1 2 4 8' OTP=1 \
    bash runtime/sglang/serve.sh

# 3) The startup log must contain:
#    [serve] ... OTP=1(lam=2.0)
#    default overthinking penalty active (lambda=2.0, think_only=True)

# 4) Ask from a SECOND terminal — plain OpenAI request, NO payload needed:
curl -s http://127.0.0.1:30000/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model": "escha-qwen38-27b-w2",
         "messages": [{"role": "user", "content": "What is 17*24? Think briefly."}],
         "max_tokens": 512, "temperature": 0.6}' | head -c 600
```

Step 1's stderr reports the token resolution
(`# resolved 49/49 overthinking markers`).
All variants, the raw CLI path and troubleshooting are in
[`USAGE-penalty.md`](USAGE-penalty.md).

---

## 7. Server launch in detail (`serve.sh`)

Example invocation from the repo root (logs to `/tmp/opencode/server.log`):

```bash
setsid nohup env MODEL=./Qwen3.8-27B-Escha-W2 VENV=.venv \
    MEM=0.92 CTXLEN=100000 HOST=0.0.0.0 API_KEY=marec \
    CUDA_GRAPH_BS='1 2 4' EXTRA_ARGS="--enable-metrics" \
    bash runtime/sglang/serve.sh >/tmp/opencode/server.log 2>&1 &
```

The most important env vars (full list in the script header):

| Var | Meaning |
|---|---|
| `MODEL` | Model directory (required) |
| `VENV` | venv to activate (here `.venv` in the repo) |
| `MEM` | `--mem-fraction-static` (KV pool size) |
| `CTXLEN` | Per-request context cap |
| `HOST` / `PORT` | Bind address (default 127.0.0.1:30000); with `0.0.0.0` set `API_KEY`! |
| `API_KEY` | Bearer key for clients |
| `CUDA_GRAPH_BS` | Captured CUDA-graph batch sizes |
| `EXTRA_ARGS` | Raw extra flags for `launch_server` (e.g. `--enable-metrics`) |
| `ENABLE_CLP` | `1` (= default) → `--enable-custom-logit-processor` — **required for BOTH penalty and budget** |
| `OTP` | `1` → server-default penalty active (see next section) |
| `PENALTY_LAMBDA` | Default λ of the injected payload (`2.0`) — only relevant with `OTP=1` |
| `PENALTY_ALWAYS` | `1` → every token instead of `<think>` only; default `0` = think-only |
| `KVDTYPE` | KV-cache dtype, default `fp8_e4m3`; see [`docs/kv-cache-quantization.md`](../../docs/kv-cache-quantization.md) |

GPU-dependent profiles (which values for which card) are covered by
[`INSTALL.md`](INSTALL.md), section *Running on your GPU*.

---

## 8. Server-default penalty (`OTP=1`)

Since the patch `patches/server-default-penalty.patch`, the server builds a
penalty payload itself at startup — same processor, same markers, just without
the CLI detour. Apply with `git apply` from the venv root; code anchors and
patch points A–D are documented in
[`DESIGN-server-default-penalty.md`](DESIGN-server-default-penalty.md).

```bash
MODEL=./Qwen3.8-27B-Escha-W2 OTP=1 bash runtime/sglang/serve.sh
# Startup log: "default overthinking penalty active (lambda=2.0, think_only=True)"
```

Properties:

- **Applies to** chat/completions, completions and `/generate` — every request
  without its own `custom_logit_processor`. Requests **with** their own payload
  remain untouched.
- **Precedence unchanged**: request-`custom_params` > server default > fallback.
  Switch off per request: `"custom_params": {"penalty_lambda": 0}`;
  combine: `{"penalty_lambda": 4.0, "thinking_budget": 28672}`.
- `OTP=1` without `ENABLE_CLP=1` aborts in `serve.sh` with an error (the
  injected payload would otherwise be a custom logit processor on a server that
  rejects those).
- **Disabled = default:** without `OTP=1` the server behaves bit-identically to
  the unpatched state.
- If the payload build fails (e.g. tokenizer without `<think>`), the server logs
  `default overthinking penalty disabled: <reason>` and keeps **running
  normally** — just without the default penalty.

Runnable example (all four variants against an `OTP=1` server):

```bash
.venv/bin/python runtime/sglang/examples/penalty_example.py [BASE_URL]
```

---

## 9. Sending requests & recipes

### 9.1 With your own payload (path A)

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
    # SGLang extensions:
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

### 9.2 Recipe table (`custom_params`)

| Goal | `custom_params` |
|---|---|
| Default (λ=2, thinking only) | omit or `{}` |
| Stronger / paper-faithful everywhere | `{"penalty_lambda": 4.0, "think_only": false}` |
| **Switch off completely** (per request) | `{"penalty_lambda": 0}` |
| Only cap the thinking | `{"penalty_lambda": 0, "thinking_budget": 28672}` |
| Both combined | `{"penalty_lambda": 2.0, "think_only": true, "thinking_budget": 28672}` |

On choosing λ: **2.0** is the paper default and was measured on THIS model.
Above 2.5 reasoning becomes noticeably terser; above 4.0 you risk suppressing
legitimate re-planning ("wait", "but"). For experimenting, a per-request
override suffices — no restart required.

---

## 10. Measuring the effect

Three ways to see it:

1. **Log line** at server startup: `default overthinking penalty active (…)`.
2. **Demo comparison** against a running server — sends off/default/strong and
   tabulates `finish_reason`, `completion_tokens`, `reasoning_chars`,
   `answer_chars`:
   ```bash
   .venv/bin/python runtime/sglang/overthinking_penalty.py \
       --model ./Qwen3.8-27B-Escha-W2 --demo
   ```
   Expected effect: fewer reasoning characters, more often `stop` instead of
   `length`.
3. **Benchmark with penalty:** `bench_with_penalty.py` injects the payload into
   **every** request of an `sglang.bench_serving` run (off-run: `--lambda 0`):
   ```bash
   .venv/bin/python runtime/sglang/bench_with_penalty.py \
       --model-dir ./Qwen3.8-27B-Escha-W2 --lambda 2.0 --think-only \
       --dataset-name random --num-prompts 100
   ```

A/B tip: send the same prompt once with `"penalty_lambda": 0` and once with the
default; compare `finish_reason` + token counts.

---

## 11. Verification

Carried out on this fork/checkpoint:

- `py_compile` clean; payload is valid JSON, dill round-trip loads the class
  (49 tokens, think IDs 248068/248069, defaults baked in correctly).
- Logic tests with fake reqs/tensors: inside-think penalized, closed-think/
  non-think untouched (think_only), `λ=0` off, `{}` params → defaults,
  `think_only:false` → always-on, request-λ override (e.g. 4.5), garbage-λ
  without crash, empty/None param list unchanged.
- Budget tests: budget exceeded + last token ≠ newline → newline forced;
  last token = newline → `</think>` forced; under budget → untouched;
  mixed batch (row A masking, row B penalty) correctly separated.
- Token resolution on the real tokenizer: 49/49, `" recheck"` cleanly skipped.
- Server patch verified against the live server (startup log, override λ=0/λ=4,
  batch n>1) — see [`DESIGN-server-default-penalty.md`](DESIGN-server-default-penalty.md).

---

## 12. Limitations & notes

- The fields `custom_logit_processor`/`custom_params` are **SGLang extensions** —
  standard OpenAI clients ignore them; LM Studio/opencode etc. do not use them
  automatically (own requests needed). That is why the common case is `OTP=1`:
  then clients need not send anything.
- dill serializes the class by value; the server must run the same SGLang fork
  (same wheel) as the payload builder — given here, since both use `.venv`
  resp. this wheel.
- The markers only work as **single tokens**: hence `" recheck"` is missing
  (2 tokens).
- Negative `penalty_lambda` would be technically possible (boost) and matches
  the paper's negative control condition — not recommended for production.

---

## 13. Further documentation

| Document | Content |
|---|---|
| [`USAGE-penalty.md`](USAGE-penalty.md) / [DE](USAGE-penalty.de.md) | Step-by-step usage: both activation paths, client examples, measuring the effect, troubleshooting/FAQ, compact flag reference |
| [`INSTALL.md`](INSTALL.md) / [DE](INSTALL.de.md) | Installation (requirements incl. three silent failures), GPU profiles by compute capability & VRAM, troubleshooting |
| [`DESIGN-server-default-penalty.md`](DESIGN-server-default-penalty.md) / [DE](DESIGN-server-default-penalty.de.md) | Architecture decision of the server patch: verified code anchors, patch points A–D, edge cases, rollback |
| [`../docs/kv-cache-quantization.md`](../../docs/kv-cache-quantization.md) | FP8 KV cache: research in the fork, measurements (2× context), quality A/B, HiCache failure |

