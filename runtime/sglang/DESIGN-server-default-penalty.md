<p align="right"><a href="DESIGN-server-default-penalty.de.md">🇩🇪 Deutsch</a> | <b>🇬🇧 English</b></p>

# Design: Overthinking Penalty as a Server Default (flag-controlled)

Status: 2026-08-24. Read-only analysis of the installed Escha SGLang fork
(`.venv/lib/python3.12/site-packages/sglang/`). Goal: server flags that enable
the overthinking penalty (arXiv 2606.00206) for all requests without explicit
fields; explicit request fields keep precedence.

## Table of contents

1. [Goal & assumptions](#1-goal--assumptions)
2. [Verified code anchors](#2-verified-code-anchors)
3. [Complications (already examined)](#3-complications-already-examined)
4. [Patch points (~150 lines total)](#4-patch-points-150-lines-total) — [A: `server_args.py`](#a-srtserver_argspy-12-lines) · [B: `tokenizer_manager.py`](#b-srtmanagerstokenizer_managerpy-25-lines) · [C: new module](#c-new-module-srtsamplingoverthinking_penalty_defaultpy-120-lines) · [D: `serve.sh`](#d-runtimesglangservesh-10-lines)
5. [Edge cases](#5-edge-cases)
6. [Verification](#6-verification)
7. [Rollback](#7-rollback)

---

## 1. Goal & assumptions

- Three new server flags enable the penalty as the server standard:
  - `--default-overthinking-penalty`
  - `--penalty-lambda` (default 2.0)
  - `--penalty-think-only` / `--penalty-always`
- Every request without `custom_logit_processor` automatically receives the dill
  payload (with baked-in `penalty_lambda`/`think_only`).
- Explicit request fields (`custom_logit_processor`, `custom_params`) always keep
  precedence.
- The payload is built once at server start from the tokenizer (same process, no
  CLI/file transport issues).

## 2. Verified code anchors

| Point | Location | Fact |
|---|---|---|
| Injection point | `srt/managers/tokenizer_manager.py:_validate_one_request` (line 787) | Called per request item (incl. batch, line 1038); covers chat/completions, completions and native /generate |
| Request field | `srt/managers/io_struct.py:178` | `GenerateReqInput.custom_logit_processor` is a first-class field; `_normalize_custom_logit_processor` (line 538) turns a string into a list when parallel_sample>1 |
| sampling_params | `io_struct.py:142` | raw `Dict` (or list of dicts) on the request; `custom_params` lives **inside** `sampling_params` |
| Chat path | `entrypoints/openai/protocol.py:777` | ChatRequest merges `custom_params` into the `sampling_params` dict |
| `__req__` injection | `srt/managers/schedule_batch.py:570-573` | only if `sampling_params.custom_params` is a dict (otherwise no `__req__` → `think_only` degrades to always-on) |
| CLP gate | `tokenizer_manager.py:861-862` | `custom_logit_processor` is rejected when `enable_custom_logit_processor` is off |
| Server flag | `srt/server_args.py:641` | `enable_custom_logit_processor: bool = False` — new flags go next to it |

## 3. Complications (already examined)

1. **`sampling_params` can be None** (default request) → injection must create a dict.
2. **Parallel sampling**: `sampling_params` can be a list of dicts → set `custom_params`
   in every element.
3. **ENABLE_CLP gate**: OTP=1 with ENABLE_CLP=0 would reject every request as an error →
   serve.sh must force ENABLE_CLP=1; build the payload only if both flags are set.
4. **Embedding requests** flow through the same `_validate_one_request` → inject only for
   `GenerateReqInput` (isinstance guard).
5. **`_normalize_custom_logit_processor`**: string→list at parallel_sample>1 — an injected
   string payload is compatible.
6. **Priority**: inject only at `obj.custom_logit_processor is None`; never overwrite
   `custom_params`, only add when None/missing (`setdefault`).

## 4. Patch points (~150 lines total)

All paths relative to `.venv/lib/python3.12/site-packages/sglang/`.

### A. `srt/server_args.py` (~12 lines)

After line 641 in the dataclass:
```python
default_overthinking_penalty: bool = False
penalty_lambda: float = 2.0
penalty_think_only: bool = True
```
In `add_cli_args`, next to the `--enable-custom-logit-processor` block:
`--default-overthinking-penalty` (store_true), `--penalty-lambda` (float, 2.0),
`--penalty-think-only` (store_true, default True) + `--penalty-always`
(store_false, mutually exclusive).

### B. `srt/managers/tokenizer_manager.py` (~25 lines)

`__init__` (after tokenizer load):
```python
self._default_penalty_payload: Optional[str] = None
if (self.server_args.default_overthinking_penalty
        and self.server_args.enable_custom_logit_processor
        and self.server_args.penalty_lambda != 0.0):
    try:
        self._default_penalty_payload = build_overthinking_penalty_payload(
            self.tokenizer, self.server_args.penalty_lambda,
            self.server_args.penalty_think_only)
        logger.info("default overthinking penalty active (lambda=%s, think_only=%s)", ...)
    except Exception as e:
        logger.warning("default overthinking penalty disabled: %s", e)
```

`_validate_one_request`, before line 861 (CLP validation):
```python
if isinstance(obj, GenerateReqInput) and self._default_penalty_payload:
    if obj.custom_logit_processor is None:
        obj.custom_logit_processor = self._default_penalty_payload
    sp = obj.sampling_params
    if sp is None:
        obj.sampling_params = {"custom_params": {}}
    elif isinstance(sp, dict):
        sp.setdefault("custom_params", {})
    elif isinstance(sp, list):
        for p in sp:
            p.setdefault("custom_params", {})
```

### C. New module `srt/sampling/overthinking_penalty_default.py` (~120 lines)

- `MARKERS`: 49 markers, word-identical with
  `runtime/sglang/overthinking_penalty.py:85-96`
- `build_overthinking_penalty_payload(tokenizer, default_lambda, think_only) -> str`:
  - token resolution via `tokenizer.encode` + `decode` round-trip; abort below 80% resolved
  - think IDs via `convert_tokens_to_ids` (248068/248069), newline via `encode("\n")` (198)
  - local subclass `OverthinkingPenaltyLogitProcessor(CustomLogitProcessor)` with
    `DEFAULT_PENALTY_LAMBDA`/`DEFAULT_THINK_ONLY` and the same `__call__` as the repo script
  - returns `processor_cls.to_str()`
- Abort on missing think tokens (not a thinking model → error instead of silence)

### D. `runtime/sglang/serve.sh` (~10 lines)

New env vars next to `ENABLE_CLP`:
```bash
OTP=${OTP:-0}
PENALTY_LAMBDA=${PENALTY_LAMBDA:-2.0}
PENALTY_ALWAYS=${PENALTY_ALWAYS:-0}
OTP_ARGS=()
if [[ "$OTP" == "1" ]]; then
  [[ "$ENABLE_CLP" != "1" ]] && { echo "[serve] ERROR: OTP=1 requires ENABLE_CLP=1" >&2; exit 1; }
  OTP_ARGS+=(--default-overthinking-penalty --penalty-lambda "$PENALTY_LAMBDA")
  [[ "$PENALTY_ALWAYS" == "1" ]] && OTP_ARGS+=(--penalty-always)
fi
```
`"${OTP_ARGS[@]}"` into the exec line after `"${CLP_ARGS[@]}"`.

## 5. Edge cases

- **λ=0**: the payload is not built at startup at all (guard `penalty_lambda != 0.0`)
  → behaviour bit-identical to OTP=0. Per-request `custom_params={"penalty_lambda": 0}`
  on an OTP server: processor skips at `lam == 0.0` → penalty off, budget stays active.
- **Missing `__req__`**: injection B forces `custom_params={}` for default requests →
  `schedule_batch.py` injects `__req__` → `think_only` works correctly.
- **Both request fields set**: no injection, `custom_params` untouched → request
  precedence fully preserved (chain: request `custom_params` > baked-in defaults in payload).
- **Payload build failure** (e.g. model without think tokens): try/except in `__init__`
  → warning, `_default_penalty_payload=None`, server starts normally without penalty.

## 6. Verification

1. `py_compile` of the 3 patched files + the new module
2. Start server with `OTP=1` → log: "default overthinking penalty active"
3. Request without fields → penalty active (finish_reason/token counts vs. an OTP=0 comparison)
4. Request with `custom_params={"penalty_lambda": 0}` → override applies, penalty off
5. Batch/parallel request (n>1) with OTP → no error (list path)

## 7. Rollback

- Archive the patch as a `.patch` file in `runtime/sglang/patches/`
- `.venv` is a copy → `git apply -R` or wheel reinstallation restores the status quo
- Default `OTP=0` changes nothing compared to today
