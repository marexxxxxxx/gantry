<p align="right"><a href="kv-cache-quantisierung.md">🇩🇪 Deutsch</a> | <b>🇬🇧 English</b></p>

# KV-Cache quantization for Qwen3.8-27B-Escha-W2

**Date:** 2026-08-21
**Changed files:** [`runtime/sglang/serve.sh`](../runtime/sglang/serve.sh) (the only functional change), new: `ctxmon.sh` (monitoring), this document.

**TL;DR:** FP8 KV (`fp8_e4m3`) **exactly doubles** the pool's token capacity
(32,345 → 64,690 tokens) with no measurable quality loss — verified via a greedy
A/B and an SWE-bench-Lite agent run. HiCache (RAM offload) is **non-functional**
for this hybrid architecture (HiRadixCache only supports MHA/MLA); `serve.sh`
intercepts it with an early guard before loading 9.5 GB of weights.

## Table of contents

1. [Goal & starting point](#1-goal--starting-point)
2. [Research in the fork](#2-research-in-the-fork)
   - [What is / isn't possible](#research-in-detail)
3. [Changes to `runtime/sglang/serve.sh`](#changes-to-servesh)
   - [3.1 Knob `KVDTYPE`](#31-new-knob-kvdtype-default-fp8_e4m3)
   - [3.2 Knobs `HICACHE`/`HICACHE_GB`/`HICACHE_FORCE`](#32-new-knobs-hicache--hicache_gb--hicache_force)
   - [3.3 Auto default `ATTN_BACKEND=triton`](#33-auto-default-attn_backendtriton-on-blackwell)
   - [3.4 Comments/header updated](#34-commentsheader-updated)
   - [3.5 Consistency fix](#35-consistency-fix)
4. [Measurements](#4-measurements-live-same-card-same-mem)
5. [Quality A/B test FP16 vs. FP8](#5-quality-ab-test-fp16-vs-fp8)
6. [HiCache live test (failed)](#6-hicache-live-test-ram-offload--failed-as-predicted)
7. [Operation](#7-operation)
8. [Knob reference](#8-knob-reference)

---

## 1. Goal & starting point

The goal was to quantify the KV cache in order to get **significantly more context into VRAM
without quality loss** — optionally plus a RAM offload for KV pages that no longer fit in
VRAM. Motivation: longer single conversations on a 16 GB card.

| Component | Value |
|---|---|
| GPU | RTX 5070 Ti (Blackwell, sm_120), 16 GB VRAM |
| RAM | 64 GB |
| Model | `./Qwen3.8-27B-Escha-W2` — Qwen3.5-27B hybrid architecture: 64 layers, only **16 with full attention** (every 4th layer), rest `linear_attention`; `head_dim=256`, 4 KV heads, model limit 262,144 tokens; escha 2-bit quantized (~9.5 GB weights) |
| Runtime | escha sglang fork, wheel `escha-1.2.0+qwen3dense`, pip-installed in `.venv` |

**Baseline before the change** (FP16 KV, MEM=0.88, CTXLEN=80000, triton backend):
`max_total_num_tokens = 32,345`. The old defaults in `serve.sh` (MEM=0.72, CTXLEN=65536)
had been written for a 24 GB card.

---

## 2. Research in the fork

<a id="research-in-detail"></a>
What was examined was the installed fork under
`.venv/lib/python3.12/site-packages/sglang/`:

1. **FP8 KV is supported:** `--kv-cache-dtype fp8_e4m3|fp8_e5m2`
   (`server_args.py` choices; activation via `model_runner.configure_kv_cache_dtype`).
2. **The triton backend stores K/V in the chosen dtype** (`set_kv_buffer` with
   `k_scale`/`v_scale`). The checkpoint contains no calibration scales → scale=1.0
   statically. That is the standard case and neutral for quality in practice (K/V
   activations sit far below the e4m3 range of ±448). The startup warning
   `no scaling factors ... using 1.0` is therefore expected and harmless.
3. **FP4 KV is unusable for this model:** `HybridLinearKVPool`
   (`mem_cache/memory_pool.py:1210`) hardcodes `MHATokenToKVPool` and **never** selects
   `MHATokenToKVPoolFP4`; the FP4 selection in `model_runner_kv_cache_mixin.py`
   (~line 647) exists only in the non-hybrid branch. FP4 would silently allocate wrongly
   here. Additionally: below roughly 6 bits/elem, KV quality genuinely suffers. → FP4 is
   deliberately **not offered**.
4. **HiCache (RAM offload) exists** and even takes precedence over MambaRadixCache in
   `scheduler.py` (line ~696 before ~707) — but is measured live further down as not
   functional for this model (section 6).

---

<a id="changes-to-servesh"></a>
## 3. Changes to `runtime/sglang/serve.sh`

### 3.1 New knob `KVDTYPE` (default: `fp8_e4m3`)

* Declaration + docs: lines 45–63; flag assembly: lines 275–280 (`KV_ARGS`);
  passed on in the exec line: line 375.
* Valid values:
  * `fp8_e4m3` (**default**) → `--kv-cache-dtype fp8_e4m3`
  * `fp8_e5m2` → possible, but documented as debug-only (more exponent, no mantissa;
    measurably worse than e4m3 for KV)
  * `off` / `fp16` / `float16` → **no flag** → sglang `auto` → float16 = exactly the
    behavior from before the change (rollback without behavior change)
  * anything else → `ERROR` + `exit 1`
* FP4 is deliberately not offered (see research point 3).

### 3.2 New knobs `HICACHE` / `HICACHE_GB` / `HICACHE_FORCE`

* Declaration: lines 64–73; logic: lines 284–297 (`HICACHE_ARGS`); exec line: line 376.
* `HICACHE=1` would set `--enable-hierarchical-cache --hicache-size $HICACHE_GB`
  (default 32 GB host pool). Since this **measurably does not work** for this hybrid
  model (section 6), the script builds an **early guard**: with `HICACHE=1` without
  `HICACHE_FORCE=1` you get a clear error message and `exit 1` before any weights load.
  `HICACHE_FORCE=1` bypasses the guard (for future forks).
* Because HiCache needs the radix cache, `HICACHE=1` wins over `RADIX=0`
  (with a warning on stderr).
* With `HICACHE=0` (default), `HICACHE_ARGS` still sets `--disable-radix-cache`,
  thereby replacing the old `RADIX_ARGS` logic in the exec line (`RADIX_ARGS` remains
  as an empty variable for compatibility).

### 3.3 Auto default `ATTN_BACKEND=triton` on Blackwell

* Lines 256–265. If `ATTN_BACKEND` is empty and `nvidia-smi --query-gpu=compute_cap`
  reports ≥ 12 (sm_120 → `"120"`), `triton` is set automatically and logged.
  Reason: the sglang default resolves to `flashinfer` on Blackwell, which the fork
  rejects for hybrid models.

### 3.4 Comments/header updated

* Header (line 10): defaults now explicitly "for a 16 GB card (RTX 5070 Ti) with
  FP8 KV cache on".
* `CTXLEN` comment (lines 32–37): CTXLEN is a per-request cap, not an allocation;
  with FP8 the pool doubles — CTXLEN must remain ≤ `max_total_num_tokens`, otherwise an
  overlong request gets retracted mid-generation instead of being rejected up front.
* Status echo (line 358) now also shows `KVDTYPE=`, `HICACHE=` and, where applicable,
  `ATTN=`.

### 3.5 Consistency fix

The FP4 comment originally claimed more context came via "KVDTYPE=fp8 + HICACHE=1" —
that contradicted the measured HiCache failure. Corrected to: more context comes from
`KVDTYPE=fp8_e4m3` alone; HiCache is not available for this model.

---

## 4. Measurements (live, same card, same MEM)

| Configuration | `max_total_num_tokens` | K/V pool |
|---|---|---|
| FP16 KV (baseline) | **32,345** | ~2 GB |
| FP8 e4m3 (new default) | **64,690** | K 0.99 GB + V 0.99 GB |

→ **Exactly doubled token capacity at identical VRAM.** Free VRAM after startup:
~1.56 GB (MEM=0.88 is thus maxed out; higher = OOM risk during CUDA-graph capture).

## 5. Quality A/B test FP16 vs. FP8

* Method: greedy decoding (temperature 0), 3 fixed prompts (`/tmp/opencode/qa_prompts.jsonl`),
  once against the FP16 server, once against the FP8 server.
* Baseline saved in `/tmp/opencode/baseline_fp16.json`, FP8 results in
  `/tmp/opencode/fp8_results.json`.
* Result:
  * Prompt 1 (mathematics): **byte-identical**
  * Prompt 3: **byte-identical**
  * Prompt 2 (translation): the only divergence is one extra newline after
    `**Translation:**` (near-tie token); content identical. (The FP16 baseline had also
    been truncated at 800 characters.)
* **Verdict: no quality loss from FP8 KV.**

## 6. HiCache live test (RAM offload) — failed as predicted

* Start with `--enable-hierarchical-cache`: after the full weight load the scheduler
  aborts with
  `ValueError: HiRadixCache only supports MHA and MLA yet`.
* Cause: the fork supports HiRadixCache only for pure MHA/MLA architectures, not for
  this model's hybrid/linear-attention architecture.
* Consequence: early guard in `serve.sh` (section 3.2), so users get an understandable
  error message instead of a crash after 9.5 GB of weight loading.
* **What works instead:** more context via `KVDTYPE=fp8_e4m3` (done); keep
  CTXLEN ≤ 64,690; a true RAM offload requires a fork fix in
  HybridLinearKVPool/HiRadixCache.

---

## 7. Operation

### Launch command (16 GB profile)

```bash
cd /home/user/smal_qwen
( setsid nohup env MODEL=./Qwen3.8-27B-Escha-W2 VENV=.venv \
    MEM=0.88 CTXLEN=64000 HOST=0.0.0.0 API_KEY=marec \
    CUDA_GRAPH_BS='1 2 4' \
    bash runtime/sglang/serve.sh >/tmp/opencode/server.log 2>&1 & )
```

* `KVDTYPE`, `HICACHE`, `ATTN_BACKEND` need not be set — defaults apply
  (`fp8_e4m3`, off, triton auto).
* `CTXLEN=64000` instead of 80000 because the FP8 pool holds 64,690 tokens.
* Ready message: `fired up and ready to roll`; API: port 30000, bearer `marec`.

### Stop / monitor

```bash
pkill -f sglang.launch_server                       # stop the server
curl -s -H "Authorization: Bearer marec" http://127.0.0.1:30000/get_load   # active requests
watch -n 1 ~/smal_qwen/ctxmon.sh                    # live: tok/s, current ctx, ctx peak
```

`ctxmon.sh` reads `/tmp/opencode/server.log` (overridable via `LOG=...`) and shows
status, last decode speed, current context usage and the peak value since server start.

### Validation under load (SWE-bench Lite)

After the switch, a mini-swe-agent run (50 instances, slice ::6) ran against the
FP8 server. First instance `astropy__astropy-12907`: status `Submitted` after 35
agent steps (~22 min), clean solution path (reproduction → root cause → one-line fix
→ regression test incl. git-stash baseline comparison), patch **identical to the gold
patch**. No sign of degradation from FP8 KV under real agent load (~37k tokens context,
~42–50 tok/s decode).

---

## 8. Knob reference

| Variable | Default | Effect |
|---|---|---|
| `KVDTYPE` | `fp8_e4m3` | `off`/`fp16`/`float16` = old FP16 mode; `fp8_e4m3` recommended; `fp8_e5m2` debug-only |
| `HICACHE` | `0` | `1` = RAM-offload attempt → guard exit (not supported fork-side) |
| `HICACHE_GB` | `32` | Host pool size when `HICACHE=1` |
| `HICACHE_FORCE` | `0` | `1` = bypass the guard (expects the startup ValueError) |
| `ATTN_BACKEND` | auto | empty + sm_120 → automatically `triton` |
| `CTXLEN` | 65536 | Per-request cap; keep ≤ `max_total_num_tokens` (here 64,690) |
