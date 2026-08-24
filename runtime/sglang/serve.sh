#!/usr/bin/env bash
# Serve a Qwen3.8-27B 2-/3-bit `escha` (dense) export with escha-sglang, OpenAI-compatible,
# usable by any OpenAI-compatible client. The `escha` quant method is auto-detected from
# config.json — nothing here needs to name it.
#
# Linux x86-64 + NVIDIA CUDA 12.x only (native or WSL2). See ../README.md and INSTALL.md.
#
# Required: set MODEL to your downloaded model dir (a flat folder of safetensors +
# config + tokenizer — the model repo has no nested subfolder). Everything else has
# safe, MEASURED defaults for a 16 GB card (RTX 5070 Ti) with FP8 KV cache on.
#   MODEL=./Qwen3.8-27B-Escha-W2 bash serve.sh
set -euo pipefail

# ---- paths ----------------------------------------------------------------------------------
MODEL=${MODEL:?set MODEL=/path/to/your/escha model dir}
SERVED_NAME=${SERVED_NAME:-escha-qwen38-27b-w2}   # opencode/LM Studio must use this id verbatim
VENV=${VENV:-}              # optional: path to your venv; if set, we activate it
ESCHA_SRC=${ESCHA_SRC:-}    # optional: path to escha *source* tree's src/ (only if not pip-installed)

# ---- server ---------------------------------------------------------------------------------
HOST=${HOST:-127.0.0.1}     # 0.0.0.0 to serve other machines — do this behind a VPN or tunnel and
                            #     set API_KEY; never open the raw port to the internet.
PORT=${PORT:-30000}
API_KEY=${API_KEY:-}        # REQUIRED if HOST=0.0.0.0 / exposed: clients must send this as the
                            #     Bearer key. Empty = no auth (fine only for localhost).
MEM=${MEM:-0.72}            # mem-fraction-static. 0.72 is the MEASURED default for a 24 GB card at
                            #     CTXLEN 65536 with CUDA graphs on: capture needs headroom, and 0.78
                            #     left none once the int8 head is resident. Too LOW is the other
                            #     failure mode — the server exits at startup with "Not enough memory.
                            #     Please try to increase --mem-fraction-static". If capture OOMs,
                            #     step DOWN 0.70 / 0.68 rather than up. On a >24 GB card raise it.
CTXLEN=${CTXLEN:-65536}     # the model's own limit is 262144. This is a CAP per request, not an
                            #     allocation — what actually fits is max_total_num_tokens from the
                            #     startup log. With KVDTYPE=fp8_e4m3 (default) the pool doubles;
                            #     keep CTXLEN <= that number or an over-long single request gets
                            #     retracted mid-generation instead of rejected up front.
                            #     alongside the weights. Raise on a bigger card, lower with MEM.
CHUNK=${CHUNK:-2048}        # chunked-prefill-size. Bounds the activation spike on a long prompt;
                            #     leave it alone unless you are tuning TTFT on a large card.
MAXREQ=${MAXREQ:-0}         # max concurrent requests; 0 = auto.
MAXMAMBA=${MAXMAMBA:-0}     # --max-mamba-cache-size; 0 = auto.
MAMBA_RATIO=${MAMBA_RATIO:-0.3}  # --mamba-full-memory-ratio. 0.3 (not the 0.9 default) is what
                            #     leaves the pool room for KV *and* CUDA-graph capture at CTXLEN
                            #     65536 on 24 GB. Raise it on a card with spare VRAM.
KVDTYPE=${KVDTYPE:-fp8_e4m3}  # --kv-cache-dtype: storage dtype of the full-attention KV cache.
                            #   fp8_e4m3 (DEFAULT) — KV stored as FP8 (1 byte/elem instead of 2):
                            #     the token capacity of the KV pool DOUBLES at identical VRAM,
                            #     which is the whole point on a 16 GB card. K/V are dequantized
                            #     on the fly inside the attention kernel; with no calibration
                            #     scales in the checkpoint (this one has none) scale=1.0 static
                            #     FP8 is used, which is the standard, quality-neutral-in-practice
                            #     setup (K/V activations live far below e4m3's ±448 range).
                            #     Measured on this model: max_total_num_tokens 32.3k -> ~64.7k
                            #     at unchanged MEM on a 16 GB card.
                            #   fp8_e5m2 — more exponent, NO mantissa: only useful for debugging;
                            #     measurably worse than e4m3 for KV. Prefer e4m3.
                            #   off — float16 KV as before the knob existed (= sglang "auto" here).
                            #   NOT offered: fp4/4-bit. The fork's FP4 KV pool class is not wired
                            #     into this hybrid model's HybridLinearKVPool (it would silently
                            #     fall back to a plain fp4-typed buffer and break), and below
#     ~6 bits/elem KV quality degrades for real — against this
#     script's quality-first stance. More context comes from KVDTYPE=fp8_e4m3
#     alone; HICACHE (RAM offload) is NOT available for this model, see below.
HICACHE=${HICACHE:-0}       # 1 = --enable-hierarchical-cache: evicted KV pages spill to HOST RAM
                            #     and are fetched back on demand — context beyond VRAM at zero
                            #     quality cost in theory. MEASURED ON THIS MODEL: does NOT work.
                            #     The fork's HiRadixCache rejects hybrid/linear-attention models
                            #     outright ("ValueError: HiRadixCache only supports MHA and MLA
                            #     yet") at startup, AFTER weights load. The knob stays as plumbing
                            #     for a future fork that lifts the restriction; HICACHE_FORCE=1
                            #     bypasses the early guard below if you want to try anyway.
HICACHE_GB=${HICACHE_GB:-32}  # host KV pool size in GB when HICACHE=1 (--hicache-size).
HICACHE_FORCE=${HICACHE_FORCE:-0}  # 1 = skip the HICACHE unsupported-model guard.

# ---- behavior -------------------------------------------------------------------------------
GRAPHS=${GRAPHS:-1}        # 1 = CUDA graphs ON (DEFAULT). This is a hybrid model: many small
                           #     kernels per token, so eager decode is launch-bound. Set GRAPHS=0
                           #     only to debug a capture failure; expect several-fold slower decode.
CUDA_GRAPH_BS=${CUDA_GRAPH_BS:-"1 2 4 8 12 16"}  # batch sizes captured when GRAPHS=1.
                           #     HEADS UP: at the DEFAULT MEM/MAMBA_RATIO below, a 24 GB card's
                           #     recurrent pool admits only 8-9 streams, so max_running_requests
                           #     clamps there and the 12/16 entries here are DROPPED (the startup
                           #     log prints the list actually captured). They start mattering once
                           #     you raise MAXREQ/MAXMAMBA/MEM. 32 is the decode kernel's max M, so
                           #     "1 2 4 8 12 16 24 32" captures fine on a throughput profile. Past
                           #     32 a batch falls through to a large-M path meant for prefill, and
                           #     the server refuses to capture it.
THINK=${THINK:-1}          # 1 = thinking-ON + --reasoning-parser qwen3 (simplest, always works).
                           # 0 = thinking-OFF: patches the model's chat template to default-closed
                           #     AND drops the reasoning parser.
                           # A per-request chat_template_kwargs {"enable_thinking": false} is
                           #     honoured on a THINK=1 server, so THINK=0 is not required for
                           #     clients that disable thinking per call. With thinking ON, the
                           #     reasoning arrives in `reasoning_content` and the answer in
                           #     `content`: read BOTH.
                           # This model also takes chat_template_kwargs {"reasoning_effort":
                           #     "xhigh"|"medium"|"low"} (default xhigh). An unrecognised value
                           #     makes the template raise, which surfaces as a 400.
RADIX=${RADIX:-0}          # 0 = prefix caching OFF (DEFAULT here). On this hybrid architecture the
                           #     radix cache without speculative decoding disables the overlap
                           #     scheduler, which costs more than the prefix reuse returns. Set
                           #     RADIX=1 only if you have measured a win on your own workload.
ESCHA_ROUTE=${ESCHA_ROUTE:-}  # kernel launch geometry; empty = auto-select per GPU. The two
                           #     routes are BIT-IDENTICAL, so this is purely a perf choice. On
                           #     AMPERE (sm_80/86) auto picks `lovelace` and that is the SLOWER one
                           #     at batch 1: an RTX 3090 measured 23.6 -> 40.7 tok/s (1.72x) with
                           #     ESCHA_ROUTE=blackwell. Parity at batch 2-16, so set it for
                           #     single-user serving and leave it auto for batched.
if [[ -n "$ESCHA_ROUTE" ]]; then export ESCHA_ROUTE; fi   # NB: `[ .. ] && export` would abort
                                                        # the script under `set -e` when unset.
ATTN_BACKEND=${ATTN_BACKEND:-}  # full-attention backend override. Empty = sglang default (fine on
                           #     Ada/4090). REQUIRED on consumer Blackwell (RTX 50 / sm_120): the
                           #     default resolves to flashinfer but the fork asserts triton/trtllm_mha
                           #     /fa4 for hybrid models on Blackwell -> auto-set to `triton` below
                           #     when a >= sm_120 GPU is detected and nothing was given.
TOOL_PARSER=${TOOL_PARSER:-qwen3_coder}   # matches this template's <function=..>/<parameter=..> XML.
REASONING_PARSER=${REASONING_PARSER:-qwen3}
ENABLE_CLP=${ENABLE_CLP:-1}   # 1 = --enable-custom-logit-processor (DEFAULT). Needed for the
                            #     per-request thinking budget (see thinking_budget.py); it is how
                            #     every published benchmark number for this model was produced.
OTP=${OTP:-0}               # 1 = --default-overthinking-penalty: the server injects the
                            #     overthinking penalty payload (arXiv 2606.00206; see
                            #     overthinking_penalty.py) into EVERY request that does not set
                            #     its own custom_logit_processor. Requires ENABLE_CLP=1 (the
                            #     default). Per-request custom_params still win: a client can
                            #     send {"penalty_lambda": 0} to turn it off for one request,
                            #     or {"thinking_budget": N} to also bound the CoT length.
PENALTY_LAMBDA=${PENALTY_LAMBDA:-2.0}  # default penalty strength baked into the injected
                            #     payload (paper sweep 0.5-4.0). Only used when OTP=1.
PENALTY_ALWAYS=${PENALTY_ALWAYS:-0}    # 1 = penalize at EVERY decoding step instead of only
                            #     inside <think>...</think> (paper-faithful but touches final
                            #     answers/code too). Default 0 = think-only.
TRUNCATE=${TRUNCATE:-1}    # 1 = --allow-auto-truncate (DEFAULT). A prompt longer than CTXLEN is
                           #     truncated instead of HARD-REJECTED. Without it an over-long prompt
                           #     returns an error that reads like a model failure.
JSON_WS=${JSON_WS:-1}      # 1 = --constrained-json-disable-any-whitespace (DEFAULT).
                           #     With permissive whitespace, a json_schema `integer`/`number`
                           #     property that follows a string property can take a spurious
                           #     leading `-`: the grammar allows a "\n    " branch after the
                           #     colon, and from that off-distribution state the sign/digit choice
                           #     is near-tied. Compact JSON is what structured-output consumers
                           #     want anyway. `JSON_WS=0` opts out.
DETERMINISTIC=${DETERMINISTIC:-0}  # 1 = --enable-deterministic-inference. Greedy is NOT
                           #     bit-reproducible across requests by default (fp16 accumulation
                           #     order depends on batch composition): long reasoning chains diverge
                           #     once a near-tie flips. Set 1 for eval reproducibility; expect a
                           #     throughput cost. NOT AVAILABLE on consumer Blackwell (RTX 50 /
                           #     sm_120): the deterministic attention kernel asks for 104 KB of
                           #     shared memory per block, above the sm_120 limit, and the server
                           #     exits at startup. Works on Ampere / Ada / Hopper.

# ---- environment the escha stack needs ------------------------------------------------------
[[ -n "$VENV" && -f "$VENV/bin/activate" ]] && source "$VENV/bin/activate"
PYBIN=$(command -v python)
TORCH_LIB=$("$PYBIN" -c 'import torch,os;print(os.path.join(os.path.dirname(torch.__file__),"lib"))')
export LD_LIBRARY_PATH="$TORCH_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"   # escha._C links libc10
[[ -n "$ESCHA_SRC" ]] && export PYTHONPATH="$ESCHA_SRC${PYTHONPATH:+:$PYTHONPATH}"
export SGLANG_MAMBA_CONV_DTYPE=float16          # must match --dtype float16 or causal_conv1d crashes
export SGLANG_DISABLE_CUDNN_CHECK=1
# This checkpoint is TEXT-ONLY (the vision tower is in the quant `ignore` list — the checkpoint
# has zero visual.* tensors). Default to NOT instantiating the vision tower: it saves VRAM on
# every card, and prevents an image request from silently decoding through a random-init tower
# (garbage instead of an error).
export SGLANG_VLM_TEXT_ONLY=${SGLANG_VLM_TEXT_ONLY:-1}
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True   # correct var name — cuts reserved-vs-allocated
export PYTORCH_ALLOC_CONF=expandable_segments:True        # (alias on newer torch; keep both)
export HF_HUB_OFFLINE=1

# ---- decode/prefill kernel profile ----------------------------------------------------------
# This is the exact profile every published number for this model was measured on. All five knobs
# are pinned rather than left to their defaults, because a stale value inherited from a shell
# would read as a quality regression rather than as a configuration difference.
#
# ESCHA_STRICT=1 — refuse to silently fall back to the reference decode path; raise instead.
#   The two flags are mutually exclusive AT IMPORT, so the opposite one must not be in the
#   environment: unset it rather than trusting the caller's shell.
unset ESCHA_FORCE_REF 2>/dev/null || true
export ESCHA_STRICT=${ESCHA_STRICT:-1}
# ESCHA_MULTI=1 — one fused launch per multi-shard projection (q+k+v, gate+up) instead of a
#   per-shard loop. This is the default; pinned because it is load-bearing for decode latency.
export ESCHA_MULTI=${ESCHA_MULTI:-1}
# ESCHA_PREFILL=fused — run prefill through the fused GEMM rather than a dense reconstruction.
export ESCHA_PREFILL=${ESCHA_PREFILL:-fused}
# ESCHA_PREFILL_ACC=mixed / ESCHA_PREFILL_FP16ACC=0 — prefill accumulation precision. Under
#   ESCHA_STRICT every shard's prefill goes through this path, so these govern prefill numerics
#   for the whole model. "mixed" is the validated setting.
export ESCHA_PREFILL_ACC=${ESCHA_PREFILL_ACC:-mixed}
export ESCHA_PREFILL_FP16ACC=${ESCHA_PREFILL_FP16ACC:-0}
# ESCHA_DENSE_FUSED_EPI=0 — the fused split-K epilogue tail is OFF. It is not a numerics-neutral
#   speedup on this model (a measured multi-point accuracy regression on MATH-500), and it is the
#   only process-global mutable state on the decode side: a leaked environment value would look
#   like a quantization regression. Pinned off.
export ESCHA_DENSE_FUSED_EPI=${ESCHA_DENSE_FUSED_EPI:-0}
# ESCHA_ROUTE is now a first-class knob above (see the AMPERE note there) — it is no longer
# "not set here". Other passthrough knobs, still not set here: ESCHA_KGROUP=0 reverts mixed-rate
# projections to a per-shard launch (debug only).

# ---- int8-as-stored lm_head -----------------------------------------------------------------
# The runtime can keep lm_head INT8 in VRAM ("int8-as-stored") instead of dequantizing it to fp16
# at load. On this dense model the fp16 head is ~2.4 GB of the bytes read EVERY decode step vs
# ~1.2 GB as stored, so it is worth real single-user throughput plus ~1.2 GB of VRAM. Unlike the
# mixture-of-experts runtime, only lm_head is affected here: embed_tokens is a row LOOKUP, so
# keeping it int8 would save VRAM but ~no decode bandwidth, and it stays on the dequant path.
#   INT8=auto (default) — VRAM-threshold rule: ON at <= SGLANG_INT8_AUTO_VRAM_GB (24), OFF above.
#   INT8=on   — force ON:  single-user / latency-sensitive serving on ANY card size.
#   INT8=off  — force OFF: batched / high-concurrency throughput serving on >24 GB cards.
INT8=${INT8:-auto}
case "$INT8" in
  on)  export SGLANG_INT8_LM_HEAD=1 ;;
  off) export SGLANG_INT8_LM_HEAD=0 ;;
  auto) : ;;   # leave SGLANG_INT8_LM_HEAD as the caller set it (or unset -> AUTO)
  *) echo "[serve] ERROR: INT8 must be auto|on|off (got '$INT8')" >&2; exit 1 ;;
esac

# ---- ptxas discovery ------------------------------------------------------------------------
#  * Triton — which torch already pulls in — SHIPS a matching ptxas at
#    site-packages/triton/backends/nvidia/bin/ptxas. Probe that FIRST: a CUDA toolkit is NOT a
#    requirement of this runtime.
#  * Validate whatever the caller exported. A common shell profile sets TRITON_PTXAS_PATH to the
#    CUDA *bin directory*; `[[ -x <dir> ]]` is TRUE for directories, so a naive check accepts it
#    and triton then dies mid-startup with a permission error on that directory. Require a
#    regular file (-f) AND executable (-x); if a directory was given, try <dir>/ptxas first.
_ptxas_ok() { [[ -n "${1:-}" && -f "$1" && -x "$1" ]]; }
if ! _ptxas_ok "${TRITON_PTXAS_PATH:-}"; then
  if [[ -n "${TRITON_PTXAS_PATH:-}" ]]; then
    if _ptxas_ok "${TRITON_PTXAS_PATH%/}/ptxas"; then
      TRITON_PTXAS_PATH="${TRITON_PTXAS_PATH%/}/ptxas"
      echo "[serve] TRITON_PTXAS_PATH was a directory; using $TRITON_PTXAS_PATH"
    else
      echo "[serve] ignoring TRITON_PTXAS_PATH='$TRITON_PTXAS_PATH' (not an executable file)"
      TRITON_PTXAS_PATH=""
    fi
  fi
  if [[ -z "${TRITON_PTXAS_PATH:-}" ]]; then
    for p in \
      "$("$PYBIN" -c 'import os,triton;print(os.path.join(os.path.dirname(triton.__file__),"backends","nvidia","bin","ptxas"))' 2>/dev/null)" \
      /usr/local/cuda*/bin/ptxas \
      "$(command -v ptxas 2>/dev/null)"; do
      _ptxas_ok "$p" && { TRITON_PTXAS_PATH="$p"; break; }
    done
  fi
  export TRITON_PTXAS_PATH
fi
: "${TRITON_PTXAS_PATH:?no usable ptxas found. It normally ships inside the venv with triton (a torch dependency) — check that \`python -c \"import triton\"\` works. Otherwise point TRITON_PTXAS_PATH at a ptxas BINARY (not its directory).}"

# Only if you hit it: the first time triton compiles a kernel (during warmup AND during CUDA-graph
# capture), the multi-GB scheduler must fork() to spawn ptxas. Under the default overcommit
# heuristic that fork can fail -> the server dies with a triton JIT memory-allocation failure at
# kernel-compile time. Setting vm.overcommit_memory=1 fixes it. Many boxes never hit this (a warm
# triton cache, or enough free RAM), so it is advisory. In a container the sysctl is often
# read-only — then it is the HOST's job.
if [[ "$(cat /proc/sys/vm/overcommit_memory 2>/dev/null)" == "0" ]]; then
  echo "[serve] note: if startup dies with a triton JIT memory-allocation failure at kernel-" >&2
  echo "[serve]       compile time, set 'sudo sysctl -w vm.overcommit_memory=1' (persist via" >&2
  echo "[serve]       /etc/sysctl.d/99-escha.conf). In a read-only container this must be set" >&2
  echo "[serve]       on the host; otherwise it's safe to ignore." >&2
fi

# Warn when the GPU is already substantially occupied (a foreign server / another model on a
# shared box is the #1 cause of an immediate, confusing pool-allocation failure at startup).
_USED_MB=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -dc '0-9')
if [[ -n "$_USED_MB" && "$_USED_MB" -gt 1024 ]]; then
  echo "[serve] WARNING: GPU already has ${_USED_MB} MiB in use by other processes (nvidia-smi)." >&2
  echo "[serve]          The static pool sizing assumes a free GPU — expect an allocation failure" >&2
  echo "[serve]          if another server is resident. Check: nvidia-smi" >&2
fi

# Auto backend for consumer Blackwell: on sm_120 the sglang default resolves to flashinfer, which
# the fork rejects for hybrid models (see ATTN_BACKEND above). Detect once here instead of asking
# every RTX 50xx user to memorize a flag.
if [[ -z "$ATTN_BACKEND" ]]; then
  _CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d ' .')
  if [[ -n "$_CC" && "${_CC%%?}" -ge 12 ]] 2>/dev/null; then
    ATTN_BACKEND=triton
    echo "[serve] compute capability ${_CC:0:2}.${_CC:2} detected -> ATTN_BACKEND=triton"
  fi
fi

# ---- assemble flags -------------------------------------------------------------------------
# Piecewise CUDA graphs stay OFF on this model family in every profile (the fork's piecewise path
# does not handle its positional encoding), so it is not a knob.
GRAPH_ARGS=(--disable-piecewise-cuda-graph)
[[ "$GRAPHS" == "1" ]] && GRAPH_ARGS+=(--cuda-graph-bs $CUDA_GRAPH_BS) || GRAPH_ARGS+=(--disable-cuda-graph)

# KV cache storage dtype. `off` passes no flag -> sglang "auto" -> model dtype (float16 here),
# which is the pre-KVDTYPE behavior bit-for-bit.
KV_ARGS=()
case "$KVDTYPE" in
  off|fp16|float16) : ;;
  fp8_e4m3|fp8_e5m2) KV_ARGS+=(--kv-cache-dtype "$KVDTYPE") ;;
  *) echo "[serve] ERROR: KVDTYPE must be off|fp16|fp8_e5m2|fp8_e4m3 (got '$KVDTYPE')" >&2; exit 1 ;;
esac

# Hierarchical cache needs the radix tree (mutually exclusive with --disable-radix-cache at
# startup), so HICACHE=1 wins over RADIX=0 — with a note, since RADIX=0 is the measured default.
HICACHE_ARGS=()
if [[ "$HICACHE" == "1" ]]; then
  if [[ "$HICACHE_FORCE" != "1" ]]; then
    echo "[serve] ERROR: HICACHE=1 is NOT supported for this hybrid (linear-attention) model." >&2
    echo "[serve]        The fork's HiRadixCache raises 'HiRadixCache only supports MHA and MLA" >&2
    echo "[serve]        yet' — measured. More KV context comes from KVDTYPE=fp8_e4m3 instead;" >&2
    echo "[serve]        set HICACHE_FORCE=1 to try anyway (expect the startup ValueError)." >&2
    exit 1
  fi
  [[ "$RADIX" == "0" ]] && echo "[serve] HICACHE=1 forces the radix cache ON (hierarchical cache requires it)" >&2
  HICACHE_ARGS+=(--enable-hierarchical-cache --hicache-size "$HICACHE_GB")
else
  [[ "$RADIX" == "0" ]] && HICACHE_ARGS+=(--disable-radix-cache)
fi

POOL_ARGS=()
[[ "$MAXREQ"   != "0" ]] && POOL_ARGS+=(--max-running-requests "$MAXREQ")
[[ "$MAXMAMBA" != "0" ]] && POOL_ARGS+=(--max-mamba-cache-size "$MAXMAMBA")
[[ -n "$MAMBA_RATIO" ]] && POOL_ARGS+=(--mamba-full-memory-ratio "$MAMBA_RATIO")
RADIX_ARGS=()
ATTN_ARGS=(); [[ -n "$ATTN_BACKEND" ]] && ATTN_ARGS+=(--attention-backend "$ATTN_BACKEND")
AUTH_ARGS=(); [[ -n "$API_KEY" ]] && AUTH_ARGS+=(--api-key "$API_KEY")
CLP_ARGS=(); [[ "$ENABLE_CLP" == "1" ]] && CLP_ARGS+=(--enable-custom-logit-processor)
OTP_ARGS=()
if [[ "$OTP" == "1" ]]; then
  if [[ "$ENABLE_CLP" != "1" ]]; then
    echo "[serve] ERROR: OTP=1 requires ENABLE_CLP=1 (the injected payload is a custom logit" >&2
    echo "[serve]        processor and would be rejected on every request without it)." >&2
    exit 1
  fi
  OTP_ARGS+=(--default-overthinking-penalty --penalty-lambda "$PENALTY_LAMBDA")
  [[ "$PENALTY_ALWAYS" == "1" ]] && OTP_ARGS+=(--penalty-always)
fi
METRICS_ARGS=(); [[ "${ENABLE_METRICS:-1}" == "1" ]] && METRICS_ARGS+=(--enable-metrics)   # Prometheus /metrics: gen throughput (tok/s), queue depth, token usage
TRUNC_ARGS=(); [[ "$TRUNCATE" == "1" ]] && TRUNC_ARGS+=(--allow-auto-truncate)
JSON_ARGS=(); [[ "$JSON_WS" == "1" ]] && JSON_ARGS+=(--constrained-json-disable-any-whitespace)
DET_ARGS=(); [[ "$DETERMINISTIC" == "1" ]] && DET_ARGS+=(--enable-deterministic-inference)
if [[ "$HOST" != "127.0.0.1" && -z "$API_KEY" ]]; then
  echo "[serve] WARNING: HOST=$HOST is exposed but API_KEY is empty — no auth. Set API_KEY." >&2
fi

REASON_ARGS=(); TEMPLATE_ARGS=()
if [[ "$THINK" == "1" ]]; then
  REASON_ARGS+=(--reasoning-parser "$REASONING_PARSER")
else
  # thinking-OFF: generate a patched chat template (default-closed <think>) from the model's own.
  NOTHINK="${TMPDIR:-/tmp}/escha_nothink_$(basename "$MODEL").jinja"
  "$PYBIN" - "$MODEL" "$NOTHINK" <<'PY'
import json, os, sys
mdir, out = sys.argv[1], sys.argv[2]
src = os.path.join(mdir, "chat_template.jinja")
tmpl = open(src).read() if os.path.exists(src) else json.load(open(os.path.join(mdir,"tokenizer_config.json")))["chat_template"]
# Flip the "enable_thinking" default from ON to OFF. TWO edits are needed on this template and
# only the first is mandatory:
#   (1) the generation-prompt block that decides whether <think> is left open or pre-closed;
#   (2) the reasoning-effort gate near the top, which injects a "think carefully..." system
#       instruction whenever enable_thinking is UNDEFINED. Without (2) a thinking-off server
#       still tells the model to reason at length while pre-closing the block it would reason in.
# Each edit is attempted independently; (1) is required, (2) is best-effort.
GEN_PROMPT = [
 ("{%- if enable_thinking is defined and enable_thinking is false %}\n        {{- '<think>\\n\\n</think>\\n\\n' }}\n    {%- else %}\n        {{- '<think>\\n' }}\n    {%- endif %}",
  "{%- if enable_thinking is defined and enable_thinking is true %}\n        {{- '<think>\\n' }}\n    {%- else %}\n        {{- '<think>\\n\\n</think>\\n\\n' }}\n    {%- endif %}"),
 ("{%- set enable_thinking = enable_thinking if enable_thinking is defined else true %}",
  "{%- set enable_thinking = enable_thinking if enable_thinking is defined else false %}"),
]
EFFORT_GATE = [
 ("{%- if enable_thinking is undefined or enable_thinking is true %}",
  "{%- if enable_thinking is defined and enable_thinking is true %}"),
]
patched = tmpl
for old, new in GEN_PROMPT:
    if old in patched:
        patched = patched.replace(old, new); break
else:
    sys.exit("THINK=0: could not find the enable_thinking block to patch in this model's chat "
             "template. Serve with THINK=1, or hand-patch the template and pass it via --chat-template.")
for old, new in EFFORT_GATE:
    if old in patched:
        patched = patched.replace(old, new); break
open(out, "w").write(patched)
print(out)
PY
  TEMPLATE_ARGS+=(--chat-template "$NOTHINK")   # no reasoning parser when thinking is pre-closed
fi

echo "[serve] MODEL=$MODEL NAME=$SERVED_NAME $HOST:$PORT MEM=$MEM CTX=$CTXLEN GRAPHS=$GRAPHS THINK=$THINK RADIX=$RADIX INT8=$INT8 KVDTYPE=$KVDTYPE HICACHE=$HICACHE${ATTN_BACKEND:+ ATTN=$ATTN_BACKEND}${OTP:+ OTP=1(lam=$PENALTY_LAMBDA$([ "$PENALTY_ALWAYS" == "1" ] && echo ,always))}"
echo "[serve] tool-call-parser=$TOOL_PARSER  ptxas=$TRITON_PTXAS_PATH"

exec "$PYBIN" -m sglang.launch_server \
  --model-path "$MODEL" \
  --served-model-name "$SERVED_NAME" \
  --host "$HOST" --port "$PORT" \
  --dtype float16 \
  --mem-fraction-static "$MEM" \
  --context-length "$CTXLEN" \
  --chunked-prefill-size "$CHUNK" \
  --sampling-backend pytorch \
  --trust-remote-code \
  --tool-call-parser "$TOOL_PARSER" \
  "${REASON_ARGS[@]}" \
  "${TEMPLATE_ARGS[@]}" \
  "${POOL_ARGS[@]}" \
  "${KV_ARGS[@]}" \
  "${HICACHE_ARGS[@]}" \
  "${RADIX_ARGS[@]}" \
  "${ATTN_ARGS[@]}" \
  "${AUTH_ARGS[@]}" \
  "${CLP_ARGS[@]}" \
  "${OTP_ARGS[@]}" \
  "${METRICS_ARGS[@]}" \
  "${TRUNC_ARGS[@]}" \
  "${GRAPH_ARGS[@]}" \
  "${JSON_ARGS[@]}" \
  "${DET_ARGS[@]}" \
  --log-level info \
  "$@"          # forward any extra sglang flag verbatim
