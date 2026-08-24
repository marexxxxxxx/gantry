#!/usr/bin/env python3
"""Bounded thinking for Escha `qwen3_5` models on SGLang.

Problem this solves: with thinking on, the model may spend the whole
`max_tokens` budget reasoning and never emit an answer. The response then comes
back as `finish_reason: "length"` with `content: null` (all tokens went to
`reasoning_content`), which an eval harness scores as *wrong* rather than as
*slow*, and an agent framework sees as a no-op.

A thinking budget fixes it: once the reasoning has run for N tokens, the server
forces `</think>` so the model must produce its answer inside the remaining
budget. This is SGLang's stock `ThinkingBudgetLogitProcessor`; the only reason a
helper is needed is that SGLang's built-in Qwen3 subclass hardcodes Qwen3's
token ids (151667/151668), which are wrong for this model's ~248k vocab — so we
look the ids up from the model's own tokenizer.

Serve with the processor enabled:

    ENABLE_CLP=1 MODEL=/path/to/model bash serve.sh

Emit the payload once, then reuse it for every request:

    python thinking_budget.py --model /path/to/model > clp.json

Send it (any OpenAI client — the two extra fields are SGLang extensions):

    {"messages": [...], "max_tokens": 4096,
     "chat_template_kwargs": {"enable_thinking": true, "reasoning_effort": "xhigh"},
     "custom_logit_processor": <contents of clp.json>,
     "custom_params": {"thinking_budget": 2048}}

Budget guidance: leave enough headroom between `thinking_budget` and
`max_tokens` for the answer itself — at least ~512 tokens, and far more for a
task that answers with code. Every published benchmark number for this model
was produced with `thinking_budget` 28,672 inside `max_tokens` 32,768 (so
4,096 tokens always remain for the answer), at `reasoning_effort: "xhigh"`.

`reasoning_effort` is a second, independent lever this model exposes through
`chat_template_kwargs` — `"xhigh"` (default), `"medium"` or `"low"`. It asks
for shorter reasoning; the budget *enforces* it. They compose.

Self-check (`--demo`) needs a running server and reports, for one prompt, the
finish_reason / reasoning length / answer length with and without a budget.
"""

from __future__ import annotations

import argparse
import json
import sys

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def token_ids(model_dir: str) -> tuple[int, int, int]:
    """(start, end, newline) token ids from the model's own tokenizer."""
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(model_dir, trust_remote_code=False)
    start, end = tok.convert_tokens_to_ids([_THINK_OPEN, _THINK_CLOSE])
    unk = getattr(tok, "unk_token_id", None)
    if start is None or end is None or start == unk or end == unk:
        raise SystemExit(
            f"{model_dir}: tokenizer has no {_THINK_OPEN}/{_THINK_CLOSE} tokens — "
            "this model has no thinking mode, so a thinking budget does not apply."
        )
    newline = tok.encode("\n", add_special_tokens=False)
    if len(newline) != 1:
        raise SystemExit(f"unexpected newline tokenization: {newline!r}")
    return int(start), int(end), int(newline[0])


def payload(model_dir: str) -> str:
    """The JSON string to pass as `custom_logit_processor`."""
    from sglang.srt.sampling.custom_logit_processor import (
        ThinkingBudgetLogitProcessor,
    )

    start, end, newline = token_ids(model_dir)

    # dill serializes this subclass by value; the base class is resolved by name
    # on the server, so the server must run the same SGLang version.
    class EschaThinkingBudgetLogitProcessor(ThinkingBudgetLogitProcessor):
        THINKING_START_TOKEN_ID = start
        THINKING_END_TOKEN_ID = end
        NEW_LINE_TOKEN_ID = newline

    return EschaThinkingBudgetLogitProcessor.to_str()


def demo(base_url: str, model: str, clp: str, budget: int, max_tokens: int) -> None:
    import urllib.request

    prompt = (
        "What is the Hubble constant from Planck, and from local distance-ladder "
        "measurements? Give both numbers with units."
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for label, body in (
        ("no budget ", {}),
        (f"budget {budget}", {"custom_logit_processor": clp,
                              "custom_params": {"thinking_budget": budget}}),
    ):
        req = {"model": model, "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": 0,
               "chat_template_kwargs": {"enable_thinking": True}, **body}
        with opener.open(urllib.request.Request(
                f"{base_url}/chat/completions", json.dumps(req).encode(),
                {"Content-Type": "application/json"}), timeout=1800) as r:
            out = json.load(r)
        choice = out["choices"][0]
        msg = choice["message"]
        reasoning = msg.get("reasoning_content") or ""
        content = msg.get("content") or ""
        print(f"{label}: finish_reason={choice['finish_reason']:>6} "
              f"completion_tokens={out['usage']['completion_tokens']:>6} "
              f"reasoning_chars={len(reasoning):>6} answer_chars={len(content):>5}"
              f"{'   <-- NO ANSWER' if not content else ''}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="model directory (for the tokenizer)")
    ap.add_argument("--demo", action="store_true", help="probe a running server")
    ap.add_argument("--base-url", default="http://127.0.0.1:30000/v1")
    ap.add_argument("--served-name", default="escha-qwen38-27b-w2")
    ap.add_argument("--budget", type=int, default=2048)
    ap.add_argument("--max-tokens", type=int, default=4096)
    args = ap.parse_args()

    clp = payload(args.model)
    if args.demo:
        start, end, newline = token_ids(args.model)
        print(f"# {_THINK_OPEN}={start} {_THINK_CLOSE}={end} newline={newline}",
              file=sys.stderr)
        demo(args.base_url, args.served_name, clp, args.budget, args.max_tokens)
    else:
        print(clp)


if __name__ == "__main__":
    main()
