<p align="right"><a href="USAGE-penalty.de.md">🇩🇪 Deutsch</a> | <b>🇬🇧 English</b></p>

# Overthinking Penalty — Detailed Usage

Every way to put the overthinking penalty (arXiv 2606.00206) to work on this
Qwen3.8-27B-Escha-W2 server: from the one-switch server default (`OTP=1`)
through the raw CLI invocation to per-request control. Fundamentals (marker
mechanics, parameter contract) live in [`README.md`](README.md); this document
is about **operating** it.

## Table of contents

0. [The two paths at a glance](#0-the-two-paths-at-a-glance)
1. [Quickstart](#1-quickstart-copy--run)
2. [Server default via `serve.sh`: env vars](#2-server-default-via-servesh-env-vars)
3. [Raw CLI path (without `serve.sh`)](#3-raw-cli-path-without-servesh)
4. [As a client](#4-as-a-client) — [do nothing](#41-do-nothing-the-normal-case) · [per-request control](#42-turn-downup-per-request-custom_params) · [batch & `/generate`](#43-batch-n1-and-native-generate) · [example script](#44-full-example-as-a-script) · [your own payload](#45-your-own-payload-instead-of-the-server-default-path-a)
5. [Measuring the effect](#5-measuring-the-effect)
6. [Troubleshooting / FAQ](#6-troubleshooting--faq)
7. [Flag/var reference](#7-flagvar-reference-compact)

---

## 0. The two paths at a glance

| | Path A: **payload per request** (original) | Path B: **server default `OTP=1`** (patch) |
|---|---|---|
| Who activates it? | the client (sends the payload along) | the server admin (start flag) |
| Client effort | build payload + attach to every request | **none** — plain OpenAI requests |
| Strength/scope | baked into the payload (`--lambda`, `--always`) | at server start (`PENALTY_LAMBDA`, `PENALTY_ALWAYS`) |
| Per-request changes | own values via `custom_params` | same `custom_params` override the defaults |
| Good for | benchmarks, different profiles per request | everyday use: all clients benefit without rework |

Both paths use the same processor, the same 49 markers and the same precedence:
**request-`custom_params` > baked-in defaults > fallback**.

---

## 1. Quickstart (copy & run)

```bash
cd /home/user/smal_qwen

# 1) Start the server WITH the penalty as default (16 GB profile, RTX 5070 Ti):
MODEL=./Qwen3.8-27B-Escha-W2 VENV=.venv \
    MEM=0.90 CTXLEN=16384 CUDA_GRAPH_BS='1 2 4 8' \
    OTP=1 PENALTY_LAMBDA=2.0 \
    bash runtime/sglang/serve.sh

# 2) The startup log must contain:
#    [serve] ... OTP=1(lam=2.0)
#    default overthinking penalty active (lambda=2.0, think_only=True)

# 3) Ask from a SECOND terminal — perfectly ordinary chat, NO payload:
curl -s http://127.0.0.1:30000/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model": "escha-qwen38-27b-w2",
         "messages": [{"role": "user", "content": "What is 17*24? Think briefly."}],
         "max_tokens": 512, "temperature": 0.6}' | head -c 600
```

The request above contains nothing penalty-specific — the server injects the
processor itself. That is exactly what `OTP=1` is for.

---

## 2. Server default via `serve.sh`: env vars

`serve.sh` translates these env vars into the CLI flags of path B.

```bash
# Standard: on, lambda 2.0, only inside <think>
MODEL=./Qwen3.8-27B-Escha-W2 OTP=1 bash runtime/sglang/serve.sh

# Stronger and everywhere (also outside <think>; paper-faithful):
MODEL=./Qwen3.8-27B-Escha-W2 OTP=1 PENALTY_LAMBDA=3.0 PENALTY_ALWAYS=1 \
    bash runtime/sglang/serve.sh

# Gentler (minimal intervention):
MODEL=./Qwen3.8-27B-Escha-W2 OTP=1 PENALTY_LAMBDA=1.0 \
    bash runtime/sglang/serve.sh

# Combining with the thinking-budget knob still works unchanged:
MODEL=./Qwen3.8-27B-Escha-W2 OTP=1 PENALTY_LAMBDA=2.0 INT8=on \
    MEM=0.92 CTXLEN=100000 HOST=0.0.0.0 API_KEY=marec \
    bash runtime/sglang/serve.sh
```

| Env var | Default | Meaning |
|---|---|---|
| `OTP` | `0` | `1` = inject the penalty as the server default |
| `PENALTY_LAMBDA` | `2.0` | Strength λ; paper sweep 0.5–4.0. Higher = shorter/tighter reasoning |
| `PENALTY_ALWAYS` | `0` | `1` = penalize every token, not just `<think>` tokens (also touches final answers/code!) |
| `ENABLE_CLP` | `1` | Must be `1` when `OTP=1` is set (otherwise `serve.sh` aborts with an error) |

Background on choosing λ: 2.0 is the paper default and was measured on THIS
model (see [`README.md`](README.md), demo section). Above 2.5 reasoning becomes
noticeably terser; above 4.0 you risk suppressing legitimate re-planning
("wait", "but"). To experiment: start the server at λ=2.0 and step up to 4.0
per request (section 4) — NO restart required for that.

---

## 3. Raw CLI path (without `serve.sh`)

If you start the server yourself (`python -m sglang.launch_server …`) or want
to put the flags into your own systemd unit / Docker CMD, use these directly:

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

The last four lines are the penalty part:

| Flag | Effect |
|---|---|
| `--enable-custom-logit-processor` | unlocks the mechanism (**required** for both paths) |
| `--default-overthinking-penalty` | injects the payload into every request without its own processor |
| `--penalty-lambda 2.0` | default strength of the injected payload |
| `--penalty-think-only` \| `--penalty-always` | scope: only `<think>` (default) vs. every token |

Short forms for testing without the rest of `serve.sh`:

```bash
# minimal (just check the flags exist):
python -m sglang.launch_server --help | grep -A1 "penalty\|custom-logit"

# hard paper-faithful:
... --enable-custom-logit-processor --default-overthinking-penalty \
    --penalty-lambda 4.0 --penalty-always

# back OFF (behaviour like unpatched): simply omit the two penalty
# flags — off is the default.
```

At startup the server checks the tokenizer and logs either
`default overthinking penalty active (lambda=…, think_only=True)` or
`default overthinking penalty disabled: <reason>` (e.g. tokenizer without a
`<think>` token). On `disabled`, the server keeps running NORMALLY — just
without the default penalty.

---

## 4. As a client

### 4.1 Do nothing (the normal case)

Every ordinary OpenAI-compatible call gets the penalty automatically:

```bash
curl -s http://127.0.0.1:30000/v1/chat/completions \
    -H 'Content-Type: application/json' -d '{
      "model": "escha-qwen38-27b-w2",
      "messages": [{"role": "user", "content": "Think step by step: …"}],
      "max_tokens": 4096, "temperature": 0.6,
      "chat_template_kwargs": {"enable_thinking": true}
    }'
```

### 4.2 Turn down/up per request (`custom_params`)

`custom_params` is an SGLang extension and **overrides** the server defaults
for this one request only:

```bash
# Switch the penalty completely OFF for this ONE request:
curl -s http://127.0.0.1:30000/v1/chat/completions \
    -H 'Content-Type: application/json' -d '{
      "model": "escha-qwen38-27b-w2",
      "messages": [{"role": "user", "content": "…"}],
      "custom_params": {"penalty_lambda": 0}
    }'

# Stronger than the server default:
#   … "custom_params": {"penalty_lambda": 4.0}

# Just the cap: no penalty, but the CoT ends after ~28k tokens:
#   … "custom_params": {"penalty_lambda": 0, "thinking_budget": 28672}
```

### 4.3 Batch (n>1) and native `/generate`

Both paths are patched and work unchanged:

```bash
# n=2 via chat/completions:
curl -s http://127.0.0.1:30000/v1/chat/completions \
    -H 'Content-Type: application/json' -d '{
      "model": "escha-qwen38-27b-w2",
      "messages": [{"role": "user", "content": "Count forwards."}],
      "n": 2, "max_tokens": 64, "temperature": 0
    }'

# native /generate (sampling_params instead of custom_params at top level):
curl -s http://127.0.0.1:30000/generate \
    -H 'Content-Type: application/json' -d '{
      "text": "The capital of France is",
      "sampling_params": {"max_new_tokens": 32, "temperature": 0}
    }'
```

### 4.4 Full example as a script

```bash
.venv/bin/python runtime/sglang/examples/penalty_example.py [BASE_URL]
```

Compares live: server default / override λ=0 / override λ=4 / combined with
the budget — including finish_reason, token and character counts.

### 4.5 Your own payload instead of the server default (path A)

A client that sends ITS OWN `custom_logit_processor` fully overrides the
server default for that request (own λ, own scope):

```bash
# Build the payload once:
.venv/bin/python runtime/sglang/overthinking_penalty.py \
    --model ./Qwen3.8-27B-Escha-W2 --lambda 4.0 --always > otp_always.json

# and send it along ("clp" = file contents as string):
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

## 5. Measuring the effect

1. **Log line** at startup: `default overthinking penalty active (…)`.
2. **A/B on the same prompt** (server default on, toggled off/on per request):
   `finish_reason=length` + empty `content` = the reasoning ran into the cap;
   with the penalty this should happen less often and `completion_tokens`
   should drop.
3. **Demo comparison** against a running server:
   `.venv/bin/python runtime/sglang/overthinking_penalty.py --demo \
        --model ./Qwen3.8-27B-Escha-W2 --base-url http://127.0.0.1:30000/v1`
   (sends off/default/strong and tabulates the results).

---

## 6. Troubleshooting / FAQ

| Symptom | Cause & fix |
|---|---|
| `serve.sh` aborts: "OTP=1 requires ENABLE_CLP=1" | You set `ENABLE_CLP=0`. Without CLP every request would reject the injected payload. Either leave it unset (default is 1) or use `OTP=0`. |
| Log says `default overthinking penalty disabled: …` | Payload build failed (wrong MODEL dir? tokenizer without `<think>`?). The server keeps running, just without the default penalty. |
| Client really must have NO penalty but cannot be reworked | Send requests with `"custom_params": {"penalty_lambda": 0}` — or restart the server without `OTP=1` (then bit-identical to the unpatched state). |
| Answer quality suffers / answers seem rushed | λ too high or `PENALTY_ALWAYS=1` (penalizes final answers too). Lower `PENALTY_LAMBDA` resp. switch ALWAYS off — a restart is enough. |
| I see no effect | Check the log line (is the server really running with `OTP=1`?), and A/B against λ=0 on the SAME prompt. On short non-thinking prompts (`</think>` closed early), think_only deliberately does not apply. |
| Patch gone after a wheel update | Re-apply: `git apply runtime/sglang/patches/server-default-penalty.patch` in the venv root (points A–D in [`DESIGN-server-default-penalty.md`](DESIGN-server-default-penalty.md)). |

---

## 7. Flag/var reference (compact)

**CLI:** `--enable-custom-logit-processor` · `--default-overthinking-penalty` ·
`--penalty-lambda FLOAT` (default 2.0) · `--penalty-think-only` (default) /
`--penalty-always`

**serve.sh:** `OTP` · `PENALTY_LAMBDA` · `PENALTY_ALWAYS` (+ `ENABLE_CLP` as a
precondition)

**Request:** `custom_params.penalty_lambda` (float, 0 = off) ·
`custom_params.think_only` (bool) · `custom_params.thinking_budget` (int) ·
`custom_logit_processor` (string payload, overrides the server default)


