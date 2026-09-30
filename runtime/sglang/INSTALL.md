<p align="right"><a href="INSTALL.de.md">🇩🇪 Deutsch</a> | <b>🇬🇧 English</b></p>

# Escha Runtime — install & serve

This repository is the **runtime**: one wheel (`escha-*.whl`) holding the compiled `escha` package +
a scrubbed `sglang` fork, plus the `serve.sh` launcher. It is **model-agnostic** — point it at any
Escha model directory of a supported architecture. The quantized weights are a **separate**
repository: **`EschaLabs/Qwen3.8-27B-Escha-W2`**
(<https://huggingface.co/EschaLabs/Qwen3.8-27B-Escha-W2>).

## Table of contents

1. [Requirements](#1-requirements)
2. [Installation step by step](#2-installation-step-by-step)
3. [Serving the model](#3-serving-the-model)
4. [Behaviour worth knowing](#4-behaviour-worth-knowing)
5. [Running on your GPU](#5-running-on-your-gpu) — [by compute capability](#by-compute-capability-architecture) · [by VRAM](#by-vram) · [four key settings](#four-settings-that-are-worth-getting-right) · [concurrency](#concurrency)
6. [Bounded thinking (`thinking_budget`)](#6-bounded-thinking-thinking_budget)
7. [Troubleshooting](#7-troubleshooting)

---

## 1. Requirements

**All are hard, and three of them fail quietly:**

- **Linux x86-64 with glibc ≥ 2.28.** That covers RHEL / Rocky / Alma **8** and newer, Ubuntu 20.04+,
  and Debian 10+. The wheel is tagged `manylinux_2_28_x86_64`, so pip refuses it up front on an
  older distro instead of failing later at `import escha`.
- **Python 3.12.** The wheel is built `cp312`; any other interpreter rejects it at install time
  with "not a supported wheel on this platform". (A compiled-extension wheel is tied to one CPython
  minor — there is intentionally no 3.11/3.13 build.)
- **NVIDIA GPU, compute capability 8.0–12.0.** The build is a fat binary carrying SASS for
  Ampere (`sm_80`/`sm_86`), Ada (`sm_89`, e.g. RTX 4090 / L40S), Hopper (`sm_90`), and Blackwell
  (`sm_100`/`sm_120`), plus PTX for forward-compat JIT onto newer GPUs. Older cards (`< sm_80`,
  e.g. Turing) are unsupported. Verify the installed wheel actually covers your card with
  `cuobjdump --list-elf $(python -c "import escha,os;print(os.path.dirname(escha.__file__))")/_C*.so | grep -oE 'sm_[0-9]+' | sort -u`
  — you should see all six. If it lists only one, it is a slim build and will fail on other GPUs
  with "no kernel image is available for execution on the device" at the first launch (install and
  the step-4 op-registration check still pass — the failure is at runtime).
- **A working host C compiler and Python dev headers.** Triton JIT-compiles a `cuda_utils.c` shim
  at CUDA-graph capture time and shells out to `cc`; this is separate from `ptxas` and from a CUDA
  toolkit, so "driver only, no toolkit" does not cover it. Preflight:
  ```bash
  echo '#include <Python.h>' | gcc -xc -I"$(python -c 'import sysconfig;print(sysconfig.get_paths()["include"])')" -c - -o /dev/null
  ```
- **CUDA 12.x + matching PyTorch 2.9.x-for-CUDA-12.** `escha._C` is ABI-linked to libtorch and
  `libcudart.so.12`, so the installed torch must be the CUDA-12 build of 2.9.x (a different torch
  minor can fail to load `import escha`). Needs a driver new enough for CUDA 12.x.
- **`transformers >= 5.8`.** Older releases lack the config class for this architecture. The server
  does **not** abort — it logs a single `WARNING ... config normalize FAILED`, serves with
  un-normalized architecture parameters, and then produces fluent-looking nonsense. The wheel
  **pins `transformers>=5.8`**, so a plain `pip install ./escha-*.whl` upgrades an old transformers
  for you; you only hit this if you *downgrade* transformers afterwards. If generation is garbage,
  check your transformers version *first*. (Conversely, transformers 5.x logs a scary-looking
  `WARNING ... you can try downgrading to transformers==4.57.1` about RoPE — that is a generic
  upstream sglang warning and is **safe to ignore**; do **not** downgrade.)

## 2. Installation step by step

1. `python3.12 -m venv .venv && source .venv/bin/activate`
2. `pip install -U pip wheel`
3. **Install PyTorch from the CUDA-12 index first** (it must be the cu12 build — `escha._C` is
   ABI-linked to it, and it is the one dependency that cannot come from PyPI). **Pin to 2.9.x**:
   `pip install "torch==2.9.*" --index-url https://download.pytorch.org/whl/cu128`
   A bare `torch>=2.9.0` resolves to a newer minor, and **nothing corrects it** — the wheel
   declares 61 dependencies but *no* `torch` entry, and none of its transitive deps pin one. You
   stay on the wrong minor and `import escha` fails later with `undefined symbol: _ZN3c10...`,
   because that is the C++ ABI surface PyTorch does not keep stable across minor versions.
4. Install the runtime wheel (it ships in this repo):
   `pip install ./escha-*.whl`
   — installs the `escha` runtime, the bundled `sglang` fork, **and the fork's full runtime
   dependency closure** (transformers ≥ 5.8, flashinfer, xgrammar, msgspec, uvloop, … — all pinned
   in the wheel). No separate `sglang[srt]` install is needed. Sanity-check the whole stack (all
   three should print `True`):
   ```bash
   python -c "import torch, escha, sglang; print(torch.cuda.is_available(), hasattr(torch.ops.escha, 'escham_decode_gemv'), bool(sglang.__version__))"
   ```
   (Note `import sglang` in the check — a `torch`+`escha`-only check passes on a box that cannot
   actually serve.)
5. Download a model (the weights are a **separate** repo; it is a flat folder — safetensors, config
   and tokenizer at the root, no nested subdir):
   ```bash
   pip install -U "huggingface_hub[cli]"
   hf download EschaLabs/Qwen3.8-27B-Escha-W2 --local-dir ./Qwen3.8-27B-Escha-W2
   ```
## 3. Serving the model

**Serve it** — point `MODEL` at the downloaded model directory:

```bash
MODEL=./Qwen3.8-27B-Escha-W2 bash serve.sh
```

- **Kernel route is auto-selected at runtime by GPU** — no env var needed. The kernels ship a
  single fatbin with two launch-geometry routes; `escha` queries the device compute capability
  and picks the measured-optimal one: `blackwell` (barrier-light, SM-adaptive split-K) on
  **sm_89 Ada, sm_90 Hopper, and sm_100/sm_120 Blackwell**; `lovelace` (original geometry) on
  **sm_80/sm_86 Ampere**. Both are bit-identical; force one with
  `ESCHA_ROUTE=lovelace|blackwell` to A/B on your card (Hopper's default is a best-guess —
  worth A/B'ing). Check the active route with
  `python -c "import escha,torch; torch.zeros(1,device='cuda'); print(torch.ops.escha.escham_route())"`
  (`2`=blackwell, `1`=lovelace).
- See the top of `serve.sh` for the full knob list (`MEM`, `CTXLEN`, `GRAPHS`, `THINK`, `RADIX`,
  `INT8`, `DETERMINISTIC`, …). The defaults are the profile every published number for this
  model was measured on, on a 24 GB card.
- **OpenAI-compatible endpoint on `:30000`** (`http://127.0.0.1:30000/v1`). The served model id is
  `SERVED_NAME` from `serve.sh` (override with `SERVED_NAME=...`); list it with:
  ```bash
  curl -s http://127.0.0.1:30000/v1/models | python3 -m json.tool
  ```

- **Overthinking penalty as server default:** add `OTP=1` (plus `PENALTY_LAMBDA`, `PENALTY_ALWAYS`)
  — see [`README.md`](README.md#8-server-default-penalty-otp1) and
  [`USAGE-penalty.md`](USAGE-penalty.md) for all penalty knobs.

## 4. Behaviour worth knowing

- **Thinking is per-request.** `chat_template_kwargs: {"enable_thinking": false}` is honoured
  against the default `THINK=1` server, with or without `tools`; a **top-level `enable_thinking`
  field is ignored** — it must go inside `chat_template_kwargs`. Set `THINK=0` to serve the whole
  server with thinking off.
- **`reasoning_effort` is a second lever, and the default is the expensive one.**
  `chat_template_kwargs: {"reasoning_effort": "xhigh"|"medium"|"low"}`, default **`"xhigh"`**. Each
  level is one sentence of injected system instruction: `xhigh` asks the model to validate
  assumptions and weigh alternatives, `low` asks it to keep thinking brief, and **`medium` injects
  nothing** — it is the neutral, unsteered model, not a midpoint. If `xhigh` is over-thinking your
  traffic, `medium` is usually the right first move, `low` for latency-sensitive volume. Any other
  value makes the template raise, which surfaces as an HTTP 400 — it is a typo, not a server fault.
  All three only apply while thinking is on, and none of them *bounds* anything: a
  `thinking_budget` (below) is the only hard guarantee.
- **Read `reasoning_content` AND `content`.** With thinking on, the reasoning arrives in
  `reasoning_content` and the answer in `content`. A client that reads only one of them will see
  half the response — this is the single most common integration mistake.
- **Structured output is stable.** `serve.sh` defaults `JSON_WS=1`
  (`--constrained-json-disable-any-whitespace`), which keeps `json_schema` values consistent
  run-to-run. `JSON_WS=0` opts out.
- **Over-long prompts are truncated, not rejected.** `serve.sh` defaults `TRUNCATE=1`
  (`--allow-auto-truncate`). Without it, a prompt above `CTXLEN` is hard-rejected, and a harness
  that scores the error as an answer reports it as a model failure. Set `TRUNCATE=0` if you would
  rather see the error.
- **`usage.reasoning_tokens`** counts the reasoning span on both the streaming and non-streaming
  paths, and is 0 when no reasoning parser is active. Use it to size a `thinking_budget` from
  what reasoning actually costs.
- **`escha.__version__`** — `python -c "import escha; print(escha.__version__)"`. Read from the
  installed distribution metadata, so it cannot drift from the wheel it shipped in. Paste it
  into bug reports.
- **`DETERMINISTIC=1`** sets `--enable-deterministic-inference` — **not available on consumer
  Blackwell (RTX 50 / sm_120)**, where the deterministic attention kernel exceeds the 104 KB
  shared-memory-per-block limit and the server exits at startup. `serve.sh` forwards any extra
  arguments straight to `launch_server`, so a flag it does not model is still reachable.

## 5. Running on your GPU

The weights are **10.15 GB**. What changes across cards is the **compute-capability knob** (one
attention-backend override on the newest cards) and the **VRAM budget** (context length, the
CUDA-graph batch list, and whether `lm_head` is kept int8-as-stored). The kernel launch route is
chosen for you at runtime — you never set it.

> **Measured on three cards: a 32 GB RTX 5090 (sm_120), a 24 GB RTX 4090 (sm_89) and a 24 GB
> RTX 3090 (sm_86)**; the 4090 is also where the model's thinking-on benchmarks were produced.
> Per-GPU tokens/s live on the
> [model card](https://huggingface.co/EschaLabs/Qwen3.8-27B-Escha-W2#performance-across-gpus). The
> **16 GB and 40 GB+** rows below are still *configuration* guidance derived from the model's size
> and the wheel's architecture coverage, **not measurements** — we would rather ship a gap than a
> number we have not run.

### By compute capability (architecture)

| Arch | Compute cap | Cards | What you set |
|---|---|---|---|
| Ampere | sm_80 / sm_86 | A100, A6000, RTX 3090 | route auto-selects `lovelace`, but **`ESCHA_ROUTE=blackwell` measured 1.72× faster at batch 1 on an RTX 3090** (23.6 → 40.7 tok/s; parity at batch 2–16) — set it if you serve a single user |
| Ada | sm_89 | RTX 4090, L40S | nothing — route auto-selects `blackwell` (**the validated card**) |
| Blackwell (consumer, 32 GB) | sm_120 | RTX 5090 | **`ATTN_BACKEND=triton`** — measured, see *By VRAM* below |
| Hopper | sm_90 | H100 | nothing (route defaults `blackwell`; A/B `ESCHA_ROUTE` — Hopper is a best-guess) |
| Blackwell (data-center) | sm_100 | B200 | nothing — route auto-selects `blackwell` |
| Blackwell (consumer) | sm_120 | RTX 50-series | **`ATTN_BACKEND=triton`** (the default flashinfer backend asserts on this hybrid architecture at sm_120) |

Confirm the fatbin covers your card and the active route with:
```bash
python -c "import escha,torch; torch.zeros(1,device='cuda'); print('route', torch.ops.escha.escham_route())"
# expected: route 2 (blackwell) on sm_89/90/100/120 — incl. RTX 4090/5090; route 1 (lovelace) on sm_80/86
# Both routes are bit-identical launch geometries — forcing one is a performance choice, not a
# correctness one. Worth A/B'ing on Ampere (measured 1.72x at bs=1) and on Hopper (best-guess default).
```

### By VRAM

This checkpoint is text-only, so `SGLANG_VLM_TEXT_ONLY=1` is the `serve.sh` default.

**int8-as-stored `lm_head`.** The runtime can keep `lm_head` int8 in VRAM instead of dequantizing
it to fp16 at load: ~1.2 GB instead of ~2.4 GB, read on **every** decode step, so it buys both VRAM
and single-user decode bandwidth. `INT8=auto` (the default) turns it on at ≤ 24 GB VRAM
(`SGLANG_INT8_AUTO_VRAM_GB`) and off above; `INT8=on` / `INT8=off` force it. Note this is narrower
than the mixture-of-experts runtime: on this dense model only `lm_head` is affected —
`embed_tokens` is a row *lookup*, so keeping it int8 would save VRAM but ~no decode bandwidth, and
it stays on the dequant path.

**CUDA graphs matter on this model** (`GRAPHS=1`, the `serve.sh` default). It is a hybrid
architecture that launches many small kernels per token, so eager decode is launch-bound. Use
`GRAPHS=0` only to debug a capture failure.

| VRAM | Example | Suggested launch |
|---|---|---|
| **16 GB** | RTX 5060 Ti, RTX 5080 | `ATTN_BACKEND=triton MEM=0.90 CTXLEN=16384 CUDA_GRAPH_BS="1 2 4 8" bash serve.sh` — 10.15 GB of weights leaves a small pool; drop `CTXLEN` first if capture OOMs. Untested. |
| **24 GB** | RTX 4090, RTX 3090, L40S, A6000 | `MODEL=... bash serve.sh` — the shipped defaults (`MEM=0.72 CTXLEN=65536 MAMBA_RATIO=0.3 RADIX=0 GRAPHS=1 CUDA_GRAPH_BS="1 2 4 8 12 16"`). **Validated on RTX 4090 and RTX 3090.** Single-user profile: `SERIAL=1` (the default) clamps it to **one stream**, so the `12`/`16` capture entries are dropped. For throughput use `SERIAL=0 MAXREQ=32 MAXMAMBA=32 CUDA_GRAPH_BS="1 2 4 8 12 16 24 32"` (4090: 649 tok/s @ 16; 3090: 383). On a 3090 add `ESCHA_ROUTE=blackwell` for single-user work. |
| **32 GB** | RTX 5090 | `ATTN_BACKEND=triton MEM=0.85 CTXLEN=65536 MAXREQ=32 MAXMAMBA=32 CUDA_GRAPH_BS="1 2 4 8 12 16 24 32" bash serve.sh` — **measured**: 87.1 tok/s bs=1, 955 tok/s peak at 16 streams, 28.2 GB peak of 32.6. `INT8=on` adds +7.2–7.4% decode at bs=1, paired A/B (and +12.2% KV tokens, not free VRAM — the saved ~1.2 GB is recycled into the pool). |
| **40 GB+** | A100, H100, L40S-48 | `MEM=0.85 CTXLEN=131072 bash serve.sh`; int8 auto-OFF, raise `CTXLEN`/concurrency freely. Untested. |

### Four settings that are worth getting right

1. **`CUDA_GRAPH_BS` must include your maximum batch size — and must not exceed 32.** A batch size
   that was not captured runs *eager* (the server logs `cuda graph: False` at that
   `#running-req`), which reads roughly 15% low. 32 is the decode kernel's maximum M
   (`python -c "import torch, escha; print(torch.ops.escha.escham_decode_gemv_max_m())"`); beyond it
   a batch falls through to a large-M path meant for prefill, and the runtime declines to capture a
   graph that would bake in the wrong kernel. The default list stops at 16 because that is where
   aggregate throughput peaks on a 4090 — extend it to `"1 2 4 8 12 16 24 32"` if you actually serve
   more than 16 concurrent streams.
2. **Recurrent state bounds concurrency at short context; the KV pool bounds it at long context.**
   Every stream holds ~0.15 GB of `ssm_state` regardless of context length — that is the limit with
   short prompts. With long ones the KV pool binds first: `max_total_num_tokens` (printed at
   startup) divided by your typical ISL+OSL is your real stream ceiling. **If the server logs a
   `#running-req` below the batch you requested, the surplus is queueing and your TTFT includes
   queue time** — measured on a 3090 at 2048/2048: asked 32, ran 18, mean TTFT 140 s, which reads
   like a prefill catastrophe and is not one.
3. **`RADIX=0` unless you have measured that prefix caching helps you.** On this hybrid
   architecture the radix cache is incompatible with the overlap scheduler, so `RADIX=1` *silently
   disables it* — the server logs `Disabling overlap schedule since mamba no_buffer is not
   compatible with overlap schedule`. Keep `RADIX=1` only for multi-turn agent traffic that
   genuinely reuses long prefixes.
4. **If startup OOMs, step `MEM` DOWN, not up.** `mem-fraction-static` sizes the pool the KV cache
   is carved from, so *too low* also fails, with the opposite message ("Not enough memory. Please
   try to increase --mem-fraction-static"). The two failures are: pool-too-small at startup (raise
   `MEM`) and capture-OOM after the weights load (lower `MEM`, or shorten `CUDA_GRAPH_BS`).

### Concurrency

This is a **hybrid** model: most layers are linear-attention with a per-request
recurrent-state pool, so max concurrency is governed by that pool (`MAXMAMBA`), *not* by KV memory.
The effective `max_running_requests` is clamped to `min(MAXREQ, MAXMAMBA // ratio)`, ratio ≈ 4 with
the radix cache on, ≈ 1 with `RADIX=0`. A startup log line reports the clamp.

### Single-flight mode for multi-agent clients (default)

**One GPU, several agents** (Paperclip + Hermes + OpenCode against the same server): every
in-flight generation allocates its own KV cache *and* recurrent state, so N concurrent requests
multiply VRAM — the failure mode is a mid-generation retract or OOM, not just slower decode.
`serve.sh` therefore defaults to **single-flight**: at most one request is in `running` state;
every further request **waits in the server's FIFO queue** until the slot is free.

```bash
MODEL=./Qwen3.8-27B-Escha-W2 bash serve.sh                          # SERIAL=1 (default)
SERIAL=0 MAXREQ=32 MAXMAMBA=32 CUDA_GRAPH_BS="1 2 4 8" MODEL=... bash serve.sh   # batched throughput
```

`SERIAL=1` only changes *defaults* — an explicit `MAXREQ=32` etc. still wins:

| Knob | Default under `SERIAL=1` | Effect |
|---|---|---|
| `MAXREQ` | `1` | `--max-running-requests 1` — the scheduler's running batch holds one request, the rest queue FIFO. **This is the hard cap.** |
| `MAXMAMBA` | `1` | recurrent-state pool holds one stream → effective cap `min(MAXREQ, MAXMAMBA // ratio)` stays 1 |
| `CUDA_GRAPH_BS` | `"1"` | only the batch-1 graph is captured — no VRAM spent on parallel-batch graphs |

Expected behavior: request A (long generation) runs; request B, started while A runs, **waits**
(open connection, no 429/503) and starts only after A finishes; B then succeeds. There is **no
queue-depth limit** — a waiter is only rejected when its own prompt+output exceeds the KV pool,
which is a per-request admission error, not queueing. Queue depth is observable at
`curl -s localhost:30000/metrics` (`num_running_reqs` / `num_queue_reqs`, on by default).

Client note: a waiting request holds its HTTP connection open, so agent-side timeouts must
exceed the expected queue wait (client config, not the server's).

**Timeouts:** SGLang's queue-wait and run timeouts (`SGLANG_REQ_WAITING_TIMEOUT` /
`SGLANG_REQ_RUNNING_TIMEOUT`) default to `-1` (never time out) and are pinned to `-1` in
`serve.sh`, so no waiter is ever dropped and no long generation is killed for duration. The
only timeouts that can still cut a waiting request are client-side (Paperclip / Hermes /
OpenCode) — those must be set above the expected queue wait.

Verify with:

```bash
bash runtime/sglang/verify-single-flight.sh            # defaults: port 30000, SERVED_NAME
```

It fires a long request A, starts B three seconds later, and asserts that B's first byte does
not arrive before A finishes, that B succeeds (HTTP 200, not 429/503), and that
`num_running_reqs` never exceeds 1.

## 6. Bounded thinking (`thinking_budget`)

**Read this before running any benchmark or agent against a thinking-ON server.** With thinking on,
the model can spend the entire `max_tokens` budget reasoning and never reach its answer. You get:

```
finish_reason: "length",  content: null      # all tokens went to reasoning_content
```

An eval harness scores that as **wrong**, not as slow, and an agent framework sees a no-op. Most
harnesses cap `max_tokens` at 512–4k, which is where this bites. How long the model thinks varies
run to run, so a cap that works on one prompt can fail on the next — you want a *guarantee*, not a
bigger number. `reasoning_effort: "low"` *asks* for less; it does not bound it.

A **thinking budget** is that guarantee: after N reasoning tokens the server forces `</think>`, so
the model must answer within the remaining budget. `serve.sh` enables the processor by default
(`ENABLE_CLP=1`); pass the budget per request:

```bash
MODEL=/path/to/model bash serve.sh                                 # ENABLE_CLP=1 is the default
python thinking_budget.py --model /path/to/model > clp.json        # one-time payload
```

```jsonc
{ "messages": [ /* ... */ ], "max_tokens": 32768,
  "chat_template_kwargs": {"enable_thinking": true, "reasoning_effort": "xhigh"},
  "custom_logit_processor": "<contents of clp.json>",   // SGLang extension
  "custom_params": {"thinking_budget": 28672} }
```

Leave real headroom between `thinking_budget` and `max_tokens` for the answer itself — at least
~512 tokens, and far more for a task that answers with code. **Every published thinking-on
benchmark number for this model (GPQA-Diamond, LiveCodeBench) was produced with `thinking_budget`
28,672 inside `max_tokens` 32,768** (so 4,096 tokens always remain for the answer), at
`reasoning_effort: "xhigh"`. The commonsense suite ran thinking-off and uses none of this.

`thinking_budget.py` reads `<think>` / `</think>` from your model's own tokenizer, so it works for
any Escha checkpoint that has a thinking mode. (SGLang's built-in
`Qwen3ThinkingBudgetLogitProcessor` hardcodes Qwen3's token ids, which are wrong for this model's
~248k vocabulary — hence the helper.) Verify against a running server with
`python thinking_budget.py --model <dir> --demo`.

If you would rather not think at all, `THINK=0` patches the model's own chat template to
default-closed — and also suppresses the reasoning-effort instruction the template would otherwise
inject into the system prompt — at some cost in reasoning-dependent accuracy.

## 7. Troubleshooting

- **`subprocess.CalledProcessError` from `gcc` during "Capture cuda graph".** Not an escha or
  sglang fault — your host C compiler cannot build triton's shim, and the traceback's visible
  frames (`cuda_graph_runner.py`, `triton_backend.py`) all point elsewhere. Run the compiler
  preflight in *Requirements*. On slim container images a `libisl` registered in dpkg but missing
  from disk breaks `cc1` while `gcc --version` still works.

- **`finish_reason: "length"` with `content: null` on a thinking-ON server.** The reasoning ran past
  your `max_tokens`. Raise the cap or, better, set a
  [thinking budget](#6-bounded-thinking-thinking_budget) so an answer is always produced.

- **HTTP 400 mentioning "Unexpected reasoning effort".** `reasoning_effort` must be exactly
  `xhigh`, `medium` or `low`. The chat template raises on anything else.

- **`... has no SGLang implementation and the Transformers implementation is not compatible`.** This
  error is usually a **red herring** — the fork *does* register this architecture; the real cause is
  an exception while *importing* the model module (typically an over-new `nvidia-cutlass-dsl`,
  which the wheel pins `<4.4.0`). Surface the true traceback with:
  ```bash
  python -c "import sglang.srt.models.qwen3_5"
  ```

- **`ValueError: Invalid quantization method ...`.** The wheel's registry ships exactly two
  methods, `escha` (dense) and `eschamoe` (mixture-of-experts). Check your model's
  `quantize_config.json` / `config.json` names one of them, and that you installed *this* wheel
  (`python -c "import escha; print(escha.__version__)"` should print `1.2.0+qwen3dense`) — the
  1.0.x wheels registered `eschamoe` only and cannot load a dense model at all.

- **`ModuleNotFoundError: pybase64` / `msgspec` / `xgrammar` / `flashinfer` at startup.** The wheel
  declares the full runtime dependency closure, so a clean `pip install ./escha-*.whl` pulls
  these automatically. If you see this, you likely installed the wheel with `--no-deps` or into a
  venv where pip could not resolve them — reinstall without `--no-deps`.

- **`ImportError: undefined symbol: _ZN3c10...` on `import escha`.** Wrong torch. The wheel does
  not pin it, so an unpinned install leaves you on a newer minor. Check with
  `python -c "import torch;print(torch.__version__)"` — it must be `2.9.x+cu12`. Fix:
  `pip install "torch==2.9.*" --index-url https://download.pytorch.org/whl/cu128`.

- **`PermissionError` on a CUDA `bin` directory during startup.** `TRITON_PTXAS_PATH` must point at
  the ptxas **binary**, not at the directory containing it — `[[ -x <dir> ]]` is true for
  directories, so a shell profile that sets the directory passes a naive check and then fails
  inside triton. `serve.sh` detects and repairs this case; if you launch `launch_server` directly,
  fix the variable yourself (or just unset it — triton ships its own ptxas).

- **A local benchmark client reporting HTTP 504 on `127.0.0.1`.** Check for an `HTTP_PROXY` /
  `ALL_PROXY` in your environment without `no_proxy=127.0.0.1`. Proxies time out multi-minute
  requests and synthesize a 504 while the server is still working fine.

- **OOM at startup naming a PID that is not yours.** A previous `sglang::scheduler` child is still
  holding the GPU — killing the `launch_server` parent does not reap it. Check
  `nvidia-smi --query-compute-apps=pid,used_memory --format=csv` and kill that PID. (Match on the
  process *name*; a `pkill -f sglang` pattern also matches the shell running it.)

- **Garbage / fluent-nonsense generations.** Check `transformers` first (must be ≥ 5.8); the
  transformers 5.x "downgrade to 4.57.1" RoPE warning is benign — do not act on it.

- **Two greedy requests give different text.** Expected: greedy is not bit-reproducible across
  requests, because fp16 accumulation order depends on batch composition and a long reasoning chain
  diverges once a near-tie flips. Use `DETERMINISTIC=1` when you need reproducibility.

- **Structured output (`response_format` / JSON schema) on a memory-tight card.** The grammar path
  needs ~0.5 GB of extra headroom the first time it runs (xgrammar + its sampler bookkeeping). On a
  card packed to the edge, budget for it — lower `CTXLEN` a notch if a first structured request is
  tight.
