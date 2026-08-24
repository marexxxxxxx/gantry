#!/usr/bin/env python3
"""Benchmark sglang.bench_serving with the overthinking penalty baked in.

Runs `python -m sglang.bench_serving` and injects, into EVERY request, the
overthinking-penalty `custom_logit_processor` payload plus `custom_params`,
so the penalty behaves like a server-side default for the whole benchmark.

Usage (server must already be running):

    .venv/bin/python runtime/sglang/bench_with_penalty.py \
        --model-dir ./Qwen3.8-27B-Escha-W2 \
        --lambda 2.0 --think-only \
        --dataset-name random --num-prompts 100

Everything after your flags is passed through to bench_serving unchanged
(--dataset-name, --num-prompts, --max-tokens, --base-url, ...).

Off-run (no penalty): pass --lambda 0.
"""
from __future__ import annotations

import json
import subprocess
import sys

import overthinking_penalty as otp


def main() -> None:
    argv = sys.argv[1:]
    # Parse only our own flags; keep the rest for bench_serving.
    own = {
        "--model-dir": None,
        "--lambda": None,
        "-l": None,
        "--served-name": None,
    }
    lam, think_only, model_dir, served_name = 2.0, True, None, "escha-qwen38-27b-w2"
    passthrough: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in own:
            if own[a] is not None:  # --model-dir needs a value
                own[a] = argv[i + 1]
                i += 2
                continue
            i += 1
            continue
        if a in ("--lambda", "-l"):
            lam = float(argv[i + 1])
            i += 2
            continue
        if a == "--think-only":
            think_only = True
            i += 1
            continue
        if a == "--always":
            think_only = False
            i += 1
            continue
        passthrough.append(a)
        i += 1

    model_dir = own["--model-dir"] or sys.exit(
        "--model-dir is required (tokenizer directory)")
    if own["--served-name"]:
        served_name = own["--served-name"]

    payload = otp.payload(model_dir, lam, think_only)
    extra = {
        "custom_logit_processor": payload,
        "custom_params": {"penalty_lambda": lam, "think_only": think_only},
        "chat_template_kwargs": {"enable_thinking": True},
    }

    cmd = [
        sys.executable, "-m", "sglang.bench_serving",
        "--model", served_name,
        "--extra-request-body", json.dumps(extra),
        *passthrough,
    ]
    print(f"# penalty: lambda={lam} think_only={think_only} "
          f"(injected into every request)", file=sys.stderr)
    print(f"# running: {' '.join(cmd[:6])} ...", file=sys.stderr)
    sys.exit(subprocess.call(cmd))


if __name__ == "__main__":
    main()
