#!/usr/bin/env python3
"""Use-cases: real tasks sent once to a running llama-server; the answer is recorded, not graded.

A use-case is a folder usecases/<id>/ with usecase.yaml (+ files/). See README "Use-cases".
Standalone: python3 bench/usecases.py --base-url http://127.0.0.1:8080 --out /tmp/x [--tags c]
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from loadtest import one_request  # noqa: E402  (reuse the SSE parser)

ROOT = Path(__file__).resolve().parent.parent
FENCE = re.compile(r"^```[^\n]*\n(.*?)^```[ \t]*$", re.S | re.M)
MARKERS = [re.compile(r"^\s*(?://|#)\s*file:\s*(\S.*?)\s*$"), re.compile(r"^\s*/\*\s*file:\s*(\S.*?)\s*\*/\s*$")]


def digest(uc_dir):
    """sha256 over usecase.yaml and every file under files/ (name + bytes), to detect later edits."""
    h = hashlib.sha256()
    for p in [uc_dir / "usecase.yaml", *sorted((uc_dir / "files").rglob("*"))]:
        if p.is_file():
            h.update(str(p.relative_to(uc_dir)).encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def render(uc_dir, spec):
    """Replace {files} by each listed file as a named fenced block (plain replace: code has braces)."""
    blocks = [f"{n}:\n```\n{(uc_dir / 'files' / n).read_text()}\n```" for n in spec.get("files", [])]
    return spec["prompt"].replace("{files}", "\n\n".join(blocks))


def load_usecases(tags, root=ROOT / "usecases"):
    ucs = []
    for f in sorted(root.glob("*/usecase.yaml")):
        spec = yaml.safe_load(f.read_text())
        if tags and not set(tags) & set(spec.get("tags", [])):
            continue
        ucs.append({"id": spec.get("id", f.parent.name), "tags": spec.get("tags", []),
                    "prompt": render(f.parent, spec), "max_tokens": spec.get("max_tokens", 4096),
                    "sha256": digest(f.parent)})
    return ucs


def extract_files(text):
    """Code blocks whose first line is `// file: x`, `# file: x` or `/* file: x */` -> {name: code}."""
    found = {}
    for m in FENCE.finditer(text):
        first, _, rest = m.group(1).partition("\n")
        for rx in MARKERS:
            hit = rx.match(first)
            if hit:
                name = os.path.basename(hit.group(1).strip().strip("`'\""))  # no path traversal
                if name not in ("", ".", ".."):
                    found[name] = rest
                break
    return found


def run_usecase(base_url, uc, out_dir, timeout=1800):
    """Send the prompt once (temperature 0, streaming) and write response/reasoning/result/extracted."""
    out_dir.mkdir(parents=True, exist_ok=True)
    r = one_request(base_url.rstrip("/") + "/v1/chat/completions", "model", uc["max_tokens"],
                    timeout, prompt=uc["prompt"], collect=True)
    (out_dir / "response.md").write_text(r.pop("content"))
    reasoning = r.pop("reasoning")
    if reasoning.strip():
        (out_dir / "reasoning.md").write_text(reasoning)
    files = extract_files((out_dir / "response.md").read_text())
    for name, code in files.items():
        (out_dir / "extracted").mkdir(exist_ok=True)
        (out_dir / "extracted" / name).write_text(code)
    r.update(usecase=uc["id"], sha256=uc["sha256"], max_tokens=uc["max_tokens"],
             extracted_files=len(files), reasoning_chars=len(reasoning))
    (out_dir / "result.json").write_text(json.dumps(r, indent=2))
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--out", required=True, help="directory; one subfolder per use-case")
    ap.add_argument("--tags", nargs="+", default=["default"])
    args = ap.parse_args()
    for uc in load_usecases(args.tags):
        r = run_usecase(args.base_url, uc, Path(args.out) / uc["id"])
        print(f"{uc['id']}: {r['finish_reason']} {r['output_tokens']} tok in {r['total_s']:.1f}s")


if __name__ == "__main__":
    main()
