#!/usr/bin/env python3
"""Benchmark every configured model that fits on this machine.

Works the same on an Azure VM and on a laptop (with Docker):
    python3 bench/run.py --machine local --tags smoke            # local smoke test
    python3 bench/run.py --machine gpu-t4 --push                 # what the VM runs

For each model: download GGUF -> llama-bench (raw engine speed) -> llama-server +
load test (serving under N parallel users) -> use-cases (real tasks, answers recorded
for manual comparison; only if the speed benchmark passed). Results are committed and pushed
after *each* model, so a spot eviction or crash loses at most one model.
"""
import argparse
import datetime as dt
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from usecases import load_usecases, run_usecase

ROOT = Path(__file__).resolve().parent.parent
IMAGE = os.environ.get("LLAMACPP_IMAGE", "ghcr.io/ggml-org/llama.cpp")
TAG_CPU = os.environ.get("LLAMACPP_TAG_CPU", "server")
TAG_GPU = os.environ.get("LLAMACPP_TAG_GPU", "server-cuda")
CTX_PER_SLOT = 4096
USECASE_DEFAULTS = {"enabled": True, "tags": ["default"], "min_tg_tok_s": 3, "ctx": 32768}
PORT = 8080


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def sh(cmd, check=True, **kw):
    return subprocess.run(cmd, check=check, text=True, capture_output=True, **kw)


def http_json(url, headers=None, timeout=5):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# --------------------------------------------------------------------------- system

def azure_metadata():
    try:
        return http_json("http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01",
                         {"Metadata": "true"}, timeout=2)
    except Exception:
        return None


def azure_prices(sku, region):
    """Live Linux on-demand + spot hourly price (USD) from the public Retail Prices API."""
    flt = (f"serviceName eq 'Virtual Machines' and armSkuName eq '{sku}' "
           f"and armRegionName eq '{region}' and priceType eq 'Consumption'")
    url = "https://prices.azure.com/api/retail/prices?$filter=" + urllib.parse.quote(flt)
    try:
        items = http_json(url, timeout=15).get("Items", [])
    except Exception as e:
        return {"error": repr(e)}
    prices = {}
    for it in items:
        if "Windows" in it["productName"] or "Low Priority" in it["skuName"]:
            continue
        key = "spot_usd_h" if "Spot" in it["skuName"] else "ondemand_usd_h"
        prices[key] = it["retailPrice"]
    return prices


def gpu_info():
    if not shutil.which("nvidia-smi"):
        return []
    out = sh(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
              "--format=csv,noheader,nounits"], check=False).stdout
    gpus = []
    for line in out.strip().splitlines():
        name, mem, drv = [x.strip() for x in line.split(",")]
        gpus.append({"name": name, "memory_mb": int(mem), "driver": drv})
    return gpus


def system_info(machine, spot):
    cpu = next((l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo")
                if l.startswith("model name")), platform.processor())
    mem_kb = next(int(l.split()[1]) for l in open("/proc/meminfo") if l.startswith("MemTotal"))
    info = {
        "machine": machine,
        "cpu": cpu,
        "vcpus": os.cpu_count(),
        "ram_gb": round(mem_kb / 1024 ** 2, 1),
        "gpus": gpu_info(),
        "kernel": platform.release(),
        "azure": None,
        "prices": None,
    }
    meta = azure_metadata()
    if meta:
        info["azure"] = {"vm_size": meta["vmSize"], "region": meta["location"],
                         "priority": "spot" if spot else "regular"}
        info["prices"] = azure_prices(meta["vmSize"], meta["location"])
    return info


def fits(model, sysinfo):
    need = model["size_gb"] * 1.15 + 1.5          # weights + KV cache / runtime headroom
    if sysinfo["gpus"]:
        return need <= sum(g["memory_mb"] for g in sysinfo["gpus"]) / 1024
    return need <= sysinfo["ram_gb"] * 0.85


# --------------------------------------------------------------------------- steps

def download(model, models_dir):
    dest = models_dir / model["file"]
    if dest.exists() and dest.stat().st_size > model["size_gb"] * 0.9e9:
        return dest
    url = f"https://huggingface.co/{model['repo']}/resolve/main/{model['file']}"
    cmd = ["curl", "-fsSL", "--retry", "5", "-C", "-", "-o", str(dest), url]
    if os.environ.get("HF_TOKEN"):
        cmd[1:1] = ["-H", f"Authorization: Bearer {os.environ['HF_TOKEN']}"]
    log(f"downloading {model['repo']}/{model['file']}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    return dest


def docker_run(gpu, models_dir, *extra):
    cmd = ["docker", "run", *extra, "-v", f"{models_dir}:/models:ro"]
    if gpu:
        cmd += ["--gpus", "all"]
    return cmd


def llama_bench(model, gpu, models_dir, image):
    cmd = docker_run(gpu, models_dir, "--rm") + ["--entrypoint", "/app/llama", image, "bench",
        "-m", f"/models/{model['file']}", "-p", "512", "-n", "128", "-r", "3", "-o", "json"]
    if gpu:
        cmd += ["-ngl", "999"]
    res = sh(cmd, timeout=3600)
    return json.loads(res.stdout)


def start_server(model, gpu, models_dir, image, slots, ctx, name):
    """Start llama-server in a container and wait until /health answers."""
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    cmd = docker_run(gpu, models_dir, "-d", "--name", name, "-p", f"127.0.0.1:{PORT}:{PORT}") + [
        "--entrypoint", "/app/llama-server", image,
        "-m", f"/models/{model['file']}", "--host", "0.0.0.0", "--port", str(PORT),
        "-np", str(slots), "-c", str(ctx), "--jinja", "--no-webui"]
    if gpu:
        cmd += ["-ngl", "999"]
    sh(cmd)
    deadline = time.time() + 900
    while True:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2)
            return
        except Exception:
            running = sh(["docker", "inspect", "-f", "{{.State.Running}}", name], check=False).stdout
            if "true" not in running or time.time() > deadline:
                raise RuntimeError("llama-server failed to start")
            time.sleep(3)


def stop_server(name, out_dir, log="server.log"):
    """Save the container log into out_dir and remove the container."""
    (out_dir / log).write_text(sh(["docker", "logs", name], check=False).stderr[-200_000:])
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def serve_and_load(model, gpu, models_dir, image, concurrency, out_dir):
    slots = max(concurrency)
    name = "bench-server"
    try:
        start_server(model, gpu, models_dir, image, slots, slots * CTX_PER_SLOT, name)
        out = out_dir / "loadtest.json"
        subprocess.run([sys.executable, str(ROOT / "bench" / "loadtest.py"),
                        "--base-url", f"http://127.0.0.1:{PORT}",
                        "--concurrency", *map(str, concurrency), "--out", str(out)], check=True)
        return json.loads(out.read_text())
    finally:
        stop_server(name, out_dir)


def tg_tok_s(result):
    """llama-bench token-generation speed (tg128), or 0 if missing."""
    return next((b["avg_ts"] for b in result.get("llama_bench") or [] if b["n_gen"]), 0)


def run_usecases(model, gpu, models_dir, image, cfg, tags, mdir):
    """Serve the model with one big slot and run each matching use-case once.
    Returns the usecases_status string; a failing use-case never stops the others."""
    ucs = load_usecases(tags)
    if not ucs:
        return f"skipped: no use-case matches tags {tags}"
    name, failed = "bench-usecases", 0
    try:
        start_server(model, gpu, models_dir, image, 1, cfg["ctx"], name)
        for uc in ucs:
            log(f"{model['id']}: use-case {uc['id']}")
            try:
                r = run_usecase(f"http://127.0.0.1:{PORT}", uc, mdir / "usecases" / uc["id"])
                log(f"  {r['finish_reason']}, {r['output_tokens']} tok, {r['total_s']:.0f}s, "
                    f"{r['extracted_files']} file(s) extracted")
            except Exception as e:
                failed += 1
                log(f"  use-case {uc['id']} failed: {e!r}")
                d = mdir / "usecases" / uc["id"]
                d.mkdir(parents=True, exist_ok=True)
                (d / "error.txt").write_text(repr(e))
    except Exception as e:
        return f"error: {e!r}"
    finally:
        stop_server(name, mdir, "usecases-server.log")
    return f"error: {failed} of {len(ucs)} use-cases failed" if failed else "ok"


def git_push(paths, message):
    sh(["git", "-C", str(ROOT), "add", *map(str, paths)])
    if not sh(["git", "-C", str(ROOT), "diff", "--cached", "--quiet"], check=False).returncode:
        return
    sh(["git", "-C", str(ROOT), "commit", "-m", message])
    for attempt in range(5):
        sh(["git", "-C", str(ROOT), "pull", "--rebase", "-q"], check=False)
        if sh(["git", "-C", str(ROOT), "push", "-q"], check=False).returncode == 0:
            log("pushed results")
            return
        time.sleep(5 * (attempt + 1))
    raise RuntimeError("git push failed")


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--machine", required=True, help="machine name from config/machines.yaml, or any label")
    ap.add_argument("--tags", nargs="+", help="override model tags to run")
    ap.add_argument("--models", nargs="+", help="run only these model ids")
    ap.add_argument("--concurrency", type=int, nargs="+")
    ap.add_argument("--models-dir", default=os.environ.get("MODELS_DIR", str(ROOT / "models")))
    ap.add_argument("--usecase-tags", nargs="+", help="override use-case tags to run")
    ap.add_argument("--no-usecases", action="store_true", help="skip the use-case phase")
    ap.add_argument("--usecase-min-tok-s", type=float, help="override the tg128 gate (usecases.min_tg_tok_s)")
    ap.add_argument("--push", action="store_true", help="git commit + push after each model")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate the machine only: docker image + GPU access, no models")
    args = ap.parse_args()

    machines = yaml.safe_load((ROOT / "config/machines.yaml").read_text())
    profile = {**machines["defaults"], **machines["machines"].get(args.machine, {})}
    # shallow merge above, but `usecases` is merged one level deeper
    uc_cfg = {**USECASE_DEFAULTS, **machines["defaults"].get("usecases", {}),
              **machines["machines"].get(args.machine, {}).get("usecases", {})}
    if args.usecase_min_tok_s is not None:
        uc_cfg["min_tg_tok_s"] = args.usecase_min_tok_s
    uc_tags = args.usecase_tags or uc_cfg["tags"]
    uc_ids = [u["id"] for u in load_usecases(uc_tags)]    # recorded in every result, even if gated
    models = yaml.safe_load((ROOT / "config/models.yaml").read_text())["models"]
    tags = set(args.tags or profile["tags"])
    concurrency = args.concurrency or profile["concurrency"]
    selected = [m for m in models if (args.models and m["id"] in args.models)
                or (not args.models and tags & set(m["tags"]))]

    models_dir = Path(args.models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = ROOT / "results" / args.machine / stamp
    run_dir.mkdir(parents=True)

    sysinfo = system_info(args.machine, profile.get("spot", False))
    gpu = bool(sysinfo["gpus"])
    image = f"{IMAGE}:{TAG_GPU if gpu else TAG_CPU}"
    sysinfo.update({"image": image, "concurrency": concurrency, "started_utc": stamp,
                    "dry_run": args.dry_run})
    (run_dir / "system.json").write_text(json.dumps(sysinfo, indent=2))
    log(f"{args.machine}: {sysinfo['cpu']} | {sysinfo['ram_gb']} GB | gpus={sysinfo['gpus']} | prices={sysinfo['prices']}")

    log(f"pulling {image}")
    subprocess.run(["docker", "pull", "-q", image], check=True)
    if args.dry_run:
        r = sh(docker_run(gpu, models_dir, "--rm") + ["--entrypoint", "/app/llama", image, "version"])
        sysinfo["llama_version"] = (r.stdout + r.stderr).strip()[-500:]
        if gpu:  # proves the GPU is reachable from containers (driver + nvidia-container-toolkit)
            r = sh(docker_run(gpu, models_dir, "--rm") + ["--entrypoint", "nvidia-smi", image, "-L"])
            sysinfo["container_gpus"] = r.stdout.strip()
        (run_dir / "system.json").write_text(json.dumps(sysinfo, indent=2))
        (run_dir / "DONE").write_text(dt.datetime.now(dt.timezone.utc).isoformat())
        log(f"dry run ok: {sysinfo['llama_version']} {sysinfo.get('container_gpus', '')}")
        if args.push:
            git_push([run_dir], f"chore(results): {args.machine} dry run {stamp}")
        return
    if args.push:
        git_push([run_dir], f"chore(results): {args.machine} start run {stamp}")

    for model in selected:
        mdir = run_dir / model["id"]
        mdir.mkdir()
        result = {"model": model, "status": "ok", "usecases_status": "skipped: speed benchmark did not pass",
                  "usecases_selected": uc_ids}
        t0 = time.time()
        try:
            if not fits(model, sysinfo):
                result["status"] = "skipped: does not fit in memory"
                log(f"{model['id']}: skipped (too large)")
            else:
                path = download(model, models_dir)
                result["download_s"] = round(time.time() - t0, 1)
                log(f"{model['id']}: llama-bench")
                result["llama_bench"] = llama_bench(model, gpu, models_dir, image)
                for r in result["llama_bench"]:
                    log(f"  pp{r['n_prompt']}/tg{r['n_gen']}: {r['avg_ts']:.1f} tok/s")
                log(f"{model['id']}: load test c={concurrency}")
                result["loadtest"] = serve_and_load(model, gpu, models_dir, image, concurrency, mdir)
                if args.no_usecases or not uc_cfg["enabled"]:
                    result["usecases_status"] = "disabled"
                elif tg_tok_s(result) < uc_cfg["min_tg_tok_s"]:
                    result["usecases_status"] = (f"skipped: tg128 {tg_tok_s(result):.1f} tok/s "
                                                 f"< {uc_cfg['min_tg_tok_s']}")
                else:
                    result["usecases_status"] = run_usecases(model, gpu, models_dir, image,
                                                             uc_cfg, uc_tags, mdir)
                log(f"{model['id']}: use-cases {result['usecases_status']}")
                if os.environ.get("DELETE_MODELS_AFTER_USE") == "1":
                    path.unlink()
        except Exception as e:
            result["status"] = f"error: {e!r}"
            if isinstance(e, subprocess.CalledProcessError):
                result["stderr"] = (e.stderr or "")[-5000:]
            log(f"{model['id']}: {result['status']}")
        result["elapsed_s"] = round(time.time() - t0, 1)
        (mdir / "result.json").write_text(json.dumps(result, indent=2))
        if args.push:
            try:
                git_push([run_dir], f"chore(results): {args.machine} {model['id']} {result['status'][:40]}")
            except Exception as e:
                log(f"push failed: {e!r}")

    (run_dir / "DONE").write_text(dt.datetime.now(dt.timezone.utc).isoformat())
    if args.push:
        git_push([run_dir], f"chore(results): {args.machine} run {stamp} complete")
    log(f"done -> {run_dir.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
