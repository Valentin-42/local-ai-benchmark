#!/usr/bin/env python3
"""Aggregate every use-case result under results/<machine>/<run>/<model>/usecases/ into
results/usecases.csv (one row per use-case x model x machine x run) and results/USECASES.md
(per use-case: model x machine table, latest run, each cell links to the saved answer).

Run locally after `git pull`. Unlike report.py this scans ALL runs, not just the latest.
"""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "results"
COLS = ["usecase", "model", "machine", "run", "status", "hardware", "finish_reason", "output_tokens",
        "reasoning_chars", "ttft_s", "decode_tok_s", "total_s", "prompt_tokens",
        "extracted_files", "sha256", "response_md"]


def fmt(v, spec=".1f", unit=""):
    return "–" if v is None else f"{v:{spec}}{unit}"


def hardware(sysinfo):
    return (", ".join(g["name"] for g in sysinfo["gpus"]) or f"{sysinfo['vcpus']} vCPU") + f", {sysinfo['ram_gb']:.0f} GB RAM"


def rows():
    for sys_file in sorted(RES.glob("*/*/system.json")):
        run = sys_file.parent
        hw = hardware(json.loads(sys_file.read_text()))
        for mf in sorted(run.glob("*/result.json")):
            mdir, res = mf.parent, json.loads(mf.read_text())
            base = {"model": mdir.name, "machine": run.parent.name, "run": run.name, "hardware": hw}
            done = set()
            for d in sorted((mdir / "usecases").glob("*/")):
                done.add(d.name)
                row = {c: None for c in COLS} | base | {"usecase": d.name}
                if (d / "result.json").exists():
                    row |= json.loads((d / "result.json").read_text())
                    row |= {"usecase": d.name, "status": "ok",
                            "response_md": (d / "response.md").relative_to(RES).as_posix()}
                else:   # single use-case failed: only error.txt exists
                    err = (d / "error.txt").read_text().splitlines() if (d / "error.txt").exists() else []
                    row["status"] = "error: " + (err[0] if err else "no result")
                yield {c: row.get(c) for c in COLS}
            # use-cases that never ran: whole phase skipped/disabled/errored, or speed benchmark failed
            why = res.get("usecases_status", "unknown") if res["status"] == "ok" else f"model: {res['status']}"
            for uc in res.get("usecases_selected", []):    # absent in runs from before this field existed
                if uc not in done:
                    yield {c: None for c in COLS} | base | {"usecase": uc, "status": why}


def markdown(data):
    lines = ["# Use-case results", "",
             "Latest run per model and machine; every run is in `usecases.csv`. Non-ok cells show why there is no answer (skipped, error, model did not pass). Cells: total time · decode "
             "tok/s · finish reason (`length` = answer cut off), linking to the saved answer. "
             "Answers are not graded: open them, or diff `extracted/` against the original files.", ""]
    for uc in sorted({r["usecase"] for r in data}):
        sub = [r for r in data if r["usecase"] == uc]
        machines = sorted({r["machine"] for r in sub})
        lines += [f"## {uc}", "", "| Model | " + " | ".join(machines) + " |",
                  "|---|" + "---|" * len(machines)]
        for model in sorted({r["model"] for r in sub}):
            cells = []
            for m in machines:
                runs = sorted((r for r in sub if r["model"] == model and r["machine"] == m),
                              key=lambda r: r["run"])
                r = runs[-1] if runs else None
                cells.append("–" if not r else r["status"] if r["status"] != "ok" else f"[{fmt(r['total_s'], '.0f', 's')} · "
                             f"{fmt(r['decode_tok_s'], '.1f')} tok/s · {r['finish_reason']}]({r['response_md']})")
            lines.append(f"| {model} | " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines)


def main():
    data = list(rows())
    with open(RES / "usecases.csv", "w", newline="") as f:
        w = csv.DictWriter(f, COLS)
        w.writeheader()
        w.writerows(data)
    (RES / "USECASES.md").write_text(markdown(data) + "\n")
    print((RES / "usecases.csv").read_text())
    print((RES / "USECASES.md").read_text())


if __name__ == "__main__":
    main()
