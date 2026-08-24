#!/usr/bin/env python3
"""Example: overthinking penalty as a SERVER DEFAULT (OTP=1).

With the patched server started as

    MODEL=./Qwen3.8-27B-Escha-W2 OTP=1 bash runtime/sglang/serve.sh

every request WITHOUT a custom_logit_processor gets the penalty injected by
the server itself (log line at startup: "default overthinking penalty active").
Clients need NOTHING extra — see req() below sending a completely plain body.
Per-request custom_params still win: this script turns the penalty off, turns
it up, and combines it with a thinking budget, all without resending any
payload.

Equivalent curl for case 1:

    curl -s http://127.0.0.1:30000/v1/chat/completions -H 'Content-Type: application/json' -d '{
      "model": "escha-qwen38-27b-w2",
      "messages": [{"role": "user", "content": "What is 17*24? Think briefly."}],
      "max_tokens": 512, "temperature": 0.6
    }'
"""

import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:30000"
MODEL = "escha-qwen38-27b-w2"

PROMPT = ("What is the largest eight-digit base 2 integer? Express your "
          "answer in base 10. Think step by step.")


def ask(label, **extra):
    body = {"model": MODEL,
            "messages": [{"role": "user", "content": PROMPT}],
            "max_tokens": 4096, "temperature": 0.6, "top_p": 0.95,
            "chat_template_kwargs": {"enable_thinking": True}, **extra}
    req = urllib.request.Request(f"{BASE}/v1/chat/completions",
                                 json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3600) as r:
        out = json.loads(r.read())
    choice = out["choices"][0]
    msg = choice["message"]
    reasoning = msg.get("reasoning_content") or ""
    content = msg.get("content") or ""
    print(f"{label:<28} finish={choice['finish_reason']:>6}  "
          f"tokens={out['usage']['completion_tokens']:>5}  "
          f"reasoning_chars={len(reasoning):>6}  answer_chars={len(content):>4}"
          f"{'   <-- NO ANSWER' if not content else ''}")
    return out


# 1) PLAIN request — the server injects the penalty by itself (lambda 2.0,
#    think-only). This is the whole point of OTP=1: zero client changes.
ask("server default (lam=2.0)")

# 2) Per-request OFF via custom_params precedence (no payload resent).
ask("override lam=0 (penalty off)",
    custom_params={"penalty_lambda": 0})

# 3) Stronger than the server default.
ask("override lam=4.0", custom_params={"penalty_lambda": 4.0})

# 4) Combined with a thinking budget: hard cap on <think>, then forced close.
ask("lam=2.0 + budget=1024",
    custom_params={"penalty_lambda": 2.0, "thinking_budget": 1024})
