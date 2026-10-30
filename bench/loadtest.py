#!/usr/bin/env python3
"""Concurrency load test against any OpenAI-compatible /v1/chat/completions endpoint.

Simulates N parallel users and reports what a user actually feels (time to first
token, per-user generation speed) and what the machine delivers (aggregate
tokens/s, which drives the cost per token). Standard library only.
"""
import argparse
import json
import statistics
import threading
import time
import urllib.request

PROMPT = (
    "You are helping an engineering team. Write a detailed, well structured explanation "
    "of how a hash map works internally, covering hashing, collisions, resizing and "
    "complexity. Use several paragraphs."
)


def one_request(url, model, max_tokens, timeout, prompt=PROMPT, collect=False):
    """One streaming chat completion. collect=True also returns the text and finish_reason."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    start = time.perf_counter()
    ttft = None
    chunks = 0
    usage = None
    content, reasoning, finish = [], [], None
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            event = json.loads(data)
            if event.get("usage"):
                usage = event["usage"]
            for choice in event.get("choices", []):
                delta = choice.get("delta", {})
                if collect:
                    content.append(delta.get("content") or "")
                    reasoning.append(delta.get("reasoning_content") or "")
                    finish = choice.get("finish_reason") or finish
                if delta.get("content") or delta.get("reasoning_content"):
                    if ttft is None:
                        ttft = time.perf_counter() - start
                    chunks += 1
    total = time.perf_counter() - start
    out_tokens = (usage or {}).get("completion_tokens") or chunks
    gen_time = total - (ttft or 0)
    res = {
        "ttft_s": ttft,
        "total_s": total,
        "output_tokens": out_tokens,
        "prompt_tokens": (usage or {}).get("prompt_tokens"),
        "decode_tok_s": (out_tokens - 1) / gen_time if gen_time > 0 and out_tokens > 1 else None,
    }
    if collect:
        res.update(content="".join(content), reasoning="".join(reasoning), finish_reason=finish)
    return res


def pct(values, p):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    k = min(len(values) - 1, max(0, round(p / 100 * (len(values) - 1))))
    return values[k]


def run_level(url, model, concurrency, requests, max_tokens, timeout):
    results, errors = [], []
    lock = threading.Lock()
    todo = list(range(requests))

    def worker():
        while True:
            with lock:
                if not todo:
                    return
                todo.pop()
            try:
                r = one_request(url, model, max_tokens, timeout)
                with lock:
                    results.append(r)
            except Exception as e:  # keep going, report errors
                with lock:
                    errors.append(repr(e))

    wall_start = time.perf_counter()
    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - wall_start

    total_out = sum(r["output_tokens"] for r in results)
    return {
        "concurrency": concurrency,
        "requests_ok": len(results),
        "requests_failed": len(errors),
        "errors": errors[:5],
        "wall_s": wall,
        "aggregate_output_tok_s": total_out / wall if wall > 0 else None,
        "ttft_p50_s": pct([r["ttft_s"] for r in results], 50),
        "ttft_p95_s": pct([r["ttft_s"] for r in results], 95),
        "per_user_tok_s_p50": pct([r["decode_tok_s"] for r in results], 50),
        "per_user_tok_s_min": min((r["decode_tok_s"] for r in results if r["decode_tok_s"]), default=None),
        "mean_output_tokens": statistics.mean(r["output_tokens"] for r in results) if results else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--model", default="model")
    ap.add_argument("--concurrency", type=int, nargs="+", default=[1, 4])
    ap.add_argument("--requests-per-user", type=int, default=2)
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--timeout", type=float, default=900)
    ap.add_argument("--out", help="write JSON here (default stdout)")
    args = ap.parse_args()

    url = args.base_url.rstrip("/") + "/v1/chat/completions"
    one_request(url, args.model, 8, args.timeout)  # warm-up
    levels = []
    for c in args.concurrency:
        lvl = run_level(url, args.model, c, c * args.requests_per_user, args.max_tokens, args.timeout)
        print(f"  c={c:>3}  agg={lvl['aggregate_output_tok_s'] or 0:7.1f} tok/s  "
              f"user_p50={lvl['per_user_tok_s_p50'] or 0:6.1f} tok/s  "
              f"ttft_p50={lvl['ttft_p50_s'] or 0:5.2f}s  failed={lvl['requests_failed']}", flush=True)
        levels.append(lvl)

    out = {"max_tokens": args.max_tokens, "levels": levels}
    if args.out:
        with open(args.out, "w") as f:
            json.dump(out, f, indent=2)
    else:
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
