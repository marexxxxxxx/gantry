#!/usr/bin/env python3
"""Overthinking penalty for Escha `qwen3_5` models on SGLang (Lotfi et al. 2026).

Problem this solves: aggressive post-training quantization makes reasoning
models OVERTHINK. They reach the correct answer mid-chain-of-thought, then
sample hesitation markers ("Wait", "But", "Alternatively"), open new reasoning
branches, and talk themselves out of the answer they had already found — CoT
length explodes (up to 4.5x) while accuracy drops. On this W2 export the
failure shows up as reasoning traces that run into the context/budget limit
with `finish_reason: "length"` and `content: null`.

The fix (training-free, zero overhead, arXiv 2606.00206 "Quantized Reasoning
Models Think They Need to Think Longer, but They Do Not"): at every decoding
step subtract a fixed lambda from the logits of a curated set of 49
overthinking markers. Paper results across 5 models x 3 PTQ methods x 5
benchmarks: CoT length -12..-23%, accuracy preserved or improved (+up to
6.2pp under AWQ W3), overthinking errors up to -58%.

This script emits the dill-serialized `custom_logit_processor` payload (same
mechanism as thinking_budget.py). The processor COMBINES the penalty with the
stock thinking-budget logic, so ONE request can use both levers:

    python overthinking_penalty.py --model /path/to/model > otp.json

Serve (ENABLE_CLP=1 is already the default in serve.sh):

    MODEL=./Qwen3.8-27B-Escha-W2 VENV=.venv MEM=0.92 CTXLEN=100000 \
    HOST=0.0.0.0 API_KEY=marec CUDA_GRAPH_BS='1 2 4' EXTRA_ARGS="--enable-metrics" \
    bash runtime/sglang/serve.sh >/tmp/server.log 2>&1 &

Send it (any OpenAI client — the two extra fields are SGLang extensions):

    {
      "messages": [...],
      "custom_logit_processor": "<contents of otp.json>",
      "custom_params": {"penalty_lambda": 2.0, "think_only": true}
    }

Parameter contract (per request, via `custom_params`; every key OPTIONAL —
missing keys fall back to the baked-in defaults from the CLI flags below):

  penalty_lambda   float   Logit subtraction applied to every marker token at
                           every decoding step. 0 disables the penalty.
                           Default: value of --lambda (baked into the payload).
  think_only       bool    true  -> penalize ONLY while inside <think>...
                           </think> (final answers/code stay untouched).
                           false -> penalize at EVERY step (paper-faithful).
                           Default: value of --think-only/--always.
  thinking_budget  int     Max number of reasoning tokens after <think>; once
                           reached, generation is forced through a newline
                           step into </think> (identical semantics to the
                           stock ThinkingBudgetLogitProcessor). Unset/None
                           disables the budget. This payload therefore
                           COMBINES both levers in one processor (SGLang
                           allows only one custom_logit_processor per
                           request).

Precedence: request `custom_params` > CLI-baked default > built-in fallback.

Token research notes (verified against THIS model's ~248k tokenizer):
  * Resolution is ENCODE-BASED (`tok.encode(marker)`); convert_tokens_to_ids()
    returns None for plain-text markers on this tokenizer.
  * 49 of the paper's 50 markers are single tokens here and round-trip exactly;
    " recheck" tokenizes to two tokens and is skipped with a warning.
  * <think>=248068, </think>=248069, newline=198 — matches the fork's
    Qwen38ThinkingBudgetLogitProcessor constants.

Self-check (`--demo`) needs a running server and compares, for one reasoning
prompt: no penalty / default penalty / strong penalty — finish_reason,
reasoning length, answer length.
"""

from __future__ import annotations

import argparse
import json
import sys

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"

# The paper's 50 overthinking markers (Appendix C, Table 8); all leading-space
# variants. " recheck" is commented out: it splits into 2 tokens on this
# tokenizer (~248k vocab), so it cannot be penalized as a single logit entry.
MARKERS = [
    " perhaps", " maybe", " wait", " Wait", " actually", " hold", " Hmm",
    " hmm", " Alternatively", " alternatively", " However", " however",
    " instead", " Instead", " But", " but", " though", " although", " yet",
    " rather", " unless", " otherwise", " nonetheless", " nevertheless",
    " regardless", " still", " anyway", " Or", " or", " either", " whether",
    " uncertain", " unsure", " possibly", " might", " could", " another",
    " different", " reconsider", " rethink", " backtrack", " retry",
    # " recheck",  # multi-token on this vocab ([311, 1960]) — skipped
    " revisit", " doubt", " confused", " wrong", " mistake", " error",
    " incorrect",
]


def resolve_token_ids(model_dir: str) -> dict[str, int]:
    """Map each marker to ONE token id via encode + decode round-trip check."""
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(model_dir, trust_remote_code=False)
    resolved: dict[str, int] = {}
    skipped: list[tuple[str, list[int]]] = []
    for marker in MARKERS:
        ids = tok.encode(marker, add_special_tokens=False)
        if len(ids) == 1 and tok.decode(ids) == marker:
            resolved[marker] = int(ids[0])
        else:
            skipped.append((marker, ids))
    for marker, ids in skipped:
        print(f"# skipping {marker!r}: encodes to {ids} (not a single token)",
              file=sys.stderr)
    print(f"# resolved {len(resolved)}/{len(MARKERS)} overthinking markers",
          file=sys.stderr)
    if len(resolved) < len(MARKERS) * 0.8:
        raise SystemExit(
            f"{model_dir}: fewer than 80% of markers resolved — wrong model dir?"
        )
    return resolved


def think_token_ids(model_dir: str) -> tuple[int, int, int]:
    """(<think>, </think>, newline) token ids from the model's tokenizer."""
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(model_dir, trust_remote_code=False)
    start, end = tok.convert_tokens_to_ids([_THINK_OPEN, _THINK_CLOSE])
    unk = getattr(tok, "unk_token_id", None)
    if start is None or end is None or start == unk or end == unk:
        raise SystemExit(f"{model_dir}: tokenizer has no {_THINK_OPEN}/"
                         f"{_THINK_CLOSE} tokens — no thinking mode, penalty "
                         "in think_only mode cannot apply.")
    newline = tok.encode("\n", add_special_tokens=False)
    if len(newline) != 1:
        raise SystemExit(f"{model_dir}: newline is not a single token "
                         f"({newline}) — budget forcing cannot work.")
    return int(start), int(end), int(newline[0])


def payload(model_dir: str, default_lambda: float,
            default_think_only: bool) -> str:
    """The JSON string to pass as `custom_logit_processor`."""
    from sglang.srt.sampling.custom_logit_processor import (
        CustomLogitProcessor,
    )

    token_ids = tuple(sorted(resolve_token_ids(model_dir).values()))
    think_start, think_end, newline = think_token_ids(model_dir)

    # dill serializes this subclass by value; the base class is resolved by
    # name on the server, so the server must run the same SGLang version.
    class OverthinkingPenaltyLogitProcessor(CustomLogitProcessor):
        PENALTY_TOKEN_IDS = token_ids
        THINK_START_TOKEN_ID = think_start
        THINK_END_TOKEN_ID = think_end
        NEW_LINE_TOKEN_ID = newline
        DEFAULT_PENALTY_LAMBDA = default_lambda
        DEFAULT_THINK_ONLY = default_think_only

        def __call__(self, logits, custom_param_list=None):
            if custom_param_list is None:
                return logits
            for i, row_params in enumerate(custom_param_list):
                params = row_params or {}

                # Request state: inside an OPEN <think> block?
                req = params.get("__req__")
                cur_ids = in_open_think = None
                if req is not None:
                    cur_ids = [*req.origin_input_ids, *req.output_ids]
                    in_open_think = (
                        self.THINK_START_TOKEN_ID in cur_ids
                        and self.THINK_END_TOKEN_ID not in cur_ids)

                # ---- Part 1: thinking budget (fork semantics) ----
                budget = params.get("thinking_budget")
                if (isinstance(budget, int)
                        and not isinstance(budget, bool)
                        and budget >= 0
                        and in_open_think):
                    start_index = cur_ids.index(self.THINK_START_TOKEN_ID)
                    num_after_start = len(cur_ids) - start_index - 1
                    if num_after_start >= budget:
                        if (not req.output_ids or req.output_ids[-1]
                                != self.NEW_LINE_TOKEN_ID):
                            # Step A: force a newline before closing.
                            logits[i, :] = -float("inf")
                            logits[i, self.NEW_LINE_TOKEN_ID] = 0.0
                        else:
                            # Step B: force </think>.
                            logits[i, :] = -float("inf")
                            logits[i, self.THINK_END_TOKEN_ID] = 0.0
                        continue  # row fully masked; penalty moot

                # ---- Part 2: overthinking penalty (paper Eq. 1) ----
                lam = params.get("penalty_lambda",
                                 self.DEFAULT_PENALTY_LAMBDA)
                try:
                    lam = float(lam)
                except (TypeError, ValueError):
                    continue
                if lam == 0.0:
                    continue
                if params.get("think_only", self.DEFAULT_THINK_ONLY):
                    if in_open_think is False:
                        continue
                    # in_open_think True, or None when no __req__ was sent
                    # (custom_params omitted) -> degrade to always-on penalty
                    # instead of silently dropping the row.
                logits[i, self.PENALTY_TOKEN_IDS] -= lam
            return logits

    return OverthinkingPenaltyLogitProcessor.to_str()


def _send(base_url: str, model: str, body: dict) -> dict:
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        f"{base_url}/chat/completions", json.dumps(body).encode(),
        {"Content-Type": "application/json"})
    with opener.open(req, timeout=3600) as r:
        return json.load(r)


def demo(base_url: str, model: str, clp: str, max_tokens: int) -> None:
    prompt = (
        "What is the largest eight-digit base 2 integer? Express your answer "
        "in base 10. Think step by step."
    )
    configs = [
        ("off      ", {}),
        ("default  ", {"custom_logit_processor": clp, "custom_params": {}}),
        ("lam=4.0  ", {"custom_logit_processor": clp,
                       "custom_params": {"penalty_lambda": 4.0}}),
    ]
    for label, extra in configs:
        body = {"model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens, "temperature": 0.6, "top_p": 0.95,
                "chat_template_kwargs": {"enable_thinking": True}, **extra}
        out = _send(base_url, model, body)
        choice = out["choices"][0]
        msg = choice["message"]
        reasoning = msg.get("reasoning_content") or ""
        content = msg.get("content") or ""
        print(f"{label}: finish_reason={choice['finish_reason']:>6} "
              f"completion_tokens={out['usage']['completion_tokens']:>6} "
              f"reasoning_chars={len(reasoning):>7} "
              f"answer_chars={len(content):>5}"
              f"{'   <-- NO ANSWER' if not content else ''}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True,
                    help="model directory (for the tokenizer)")
    ap.add_argument("--lambda", "-l", dest="penalty_lambda", type=float,
                    default=2.0,
                    help="default penalty baked into the payload "
                         "(paper sweep 0.5-4.0; per-request overridable via "
                         "custom_params.penalty_lambda) [default: 2.0]")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--think-only", dest="think_only", action="store_true",
                       default=True,
                       help="penalize only inside <think> (default)")
    scope.add_argument("--always", dest="think_only", action="store_false",
                       help="penalize at every decoding step (paper-faithful)")
    ap.add_argument("--demo", action="store_true", help="probe a running server")
    ap.add_argument("--base-url", default="http://127.0.0.1:30000/v1")
    ap.add_argument("--served-name", default="escha-qwen38-27b-w2")
    ap.add_argument("--max-tokens", type=int, default=4096)
    args = ap.parse_args()

    clp = payload(args.model, args.penalty_lambda, args.think_only)
    if args.demo:
        ts, te, nl = think_token_ids(args.model)
        print(f"# {_THINK_OPEN}={ts} {_THINK_CLOSE}={te} newline={nl}",
              file=sys.stderr)
        demo(args.base_url, args.served_name, clp, args.max_tokens)
    else:
        print(clp)


if __name__ == "__main__":
    main()
