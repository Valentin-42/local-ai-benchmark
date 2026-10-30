#!/usr/bin/env python3
"""Aggregate results/<machine>/<run>/ into results/SUMMARY.md.

Run locally after `git pull`. Uses the latest run per machine.

"Usable" = a level where the median user gets >= USABLE_TOK_S tokens/s
(comfortably faster than reading speed) and p95 time-to-first-token <= USABLE_TTFT_S.
Cost per 1M output tokens assumes the machine is kept busy at that level.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USABLE_TOK_S = 10
USABLE_TTFT_S = 5


def latest_runs():
    for mdir in sorted((ROOT / "results").glob("*/")):
        runs = sorted(r for r in mdir.iterdir() if (r / "system.json").exists()
                      and not json.loads((r / "system.json").read_text()).get("dry_run"))
        if runs:
            yield runs[-1]


def cost_per_m(usd_h, tok_s):
    if not usd_h or not tok_s:
        return None
    return usd_h / (tok_s * 3600) * 1e6


def fmt(v, spec=".1f", unit=""):
    return "–" if v is None else f"{v:{spec}}{unit}"


def rows():
    for run in latest_runs():
        sysinfo = json.loads((run / "system.json").read_text())
        prices = sysinfo.get("prices") or {}
        hw = (", ".join(g["name"] for g in sysinfo["gpus"]) or f"{sysinfo['vcpus']} vCPU") + f", {sysinfo['ram_gb']:.0f} GB RAM"
        for res_file in sorted(run.glob("*/result.json")):
            res = json.loads(res_file.read_text())
            row = {"machine": sysinfo["machine"], "hw": hw, "model": res["model"]["id"],
                   "status": res["status"], "prices": prices, "run": run.name}
            for b in res.get("llama_bench") or []:
                if b["n_gen"]:
                    row["tg"] = b["avg_ts"]
                elif b["n_prompt"]:
                    row["pp"] = b["avg_ts"]
            levels = (res.get("loadtest") or {}).get("levels", [])
            usable = [l for l in levels if (l["per_user_tok_s_p50"] or 0) >= USABLE_TOK_S
                      and (l["ttft_p95_s"] or 99) <= USABLE_TTFT_S and not l["requests_failed"]]
            peak = max(levels, key=lambda l: l["aggregate_output_tok_s"] or 0, default=None)
            best = max(usable, key=lambda l: l["concurrency"], default=None)
            row["peak_tok_s"] = peak and peak["aggregate_output_tok_s"]
            row["usable_users"] = best["concurrency"] if best else 0
            row["usable_tok_s"] = best and best["aggregate_output_tok_s"]
            row["ttft"] = levels[0]["ttft_p50_s"] if levels else None
            yield row


def main():
    lines = [
        "# Benchmark summary", "",
        f"Latest run per machine. *Usable users*: highest tested concurrency where the median user "
        f"gets ≥ {USABLE_TOK_S} tok/s and p95 TTFT ≤ {USABLE_TTFT_S}s. *$/1M tok*: hourly price ÷ "
        "aggregate output throughput at that level (machine kept busy).", "",
        "| Machine | Hardware | Model | pp512 tok/s | tg128 tok/s | TTFT (1 user) | Peak agg tok/s "
        "| Usable users | Spot $/h | $/1M tok (spot) | $/1M tok (on-demand) |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows():
        if r["status"] != "ok":
            lines.append(f"| {r['machine']} | {r['hw']} | {r['model']} | {r['status']} |||||||| |")
            continue
        p = r["prices"]
        lines.append(" | ".join([
            f"| {r['machine']}", r["hw"], r["model"], fmt(r.get("pp"), ".0f"), fmt(r.get("tg")),
            fmt(r["ttft"], ".2f", "s"), fmt(r["peak_tok_s"], ".0f"), str(r["usable_users"]),
            fmt(p.get("spot_usd_h"), ".3f"),
            fmt(cost_per_m(p.get("spot_usd_h"), r["usable_tok_s"]), ".2f"),
            fmt(cost_per_m(p.get("ondemand_usd_h"), r["usable_tok_s"]), ".2f") + " |",
        ]))
    out = ROOT / "results" / "SUMMARY.md"
    out.write_text("\n".join(lines) + "\n")
    print(out.read_text())


if __name__ == "__main__":
    main()
