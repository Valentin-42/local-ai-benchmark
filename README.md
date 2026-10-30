# local-ai-benchmark

How fast and how cheap are open-weight LLMs on hardware you rent yourself?
This repo answers it reproducibly: **one command starts an Azure VM that clones
this repo, benchmarks every model that fits, pushes the results here and deletes
itself.** Repeat on as many machine types as you like.

```
laptop                          Azure (one resource group per run)
──────                          ───────────────────────────────────
infra/launch.sh gpu-t4  ──────► spot VM boots (cloud-init)
                                  ├─ git clone this repo
                                  ├─ bench/run.py: for each model
                                  │    download GGUF → llama-bench → llama-server + load test
                                  │    git commit + push   (after every model)
                                  └─ deletes its own resource group
git pull
python3 bench/report.py  ──►  results/SUMMARY.md
```

## What is measured

| Metric | Why it matters |
|---|---|
| `pp512` / `tg128` tok/s (llama-bench) | Raw prompt-processing and generation speed; comparable with community numbers |
| TTFT at 1 user | Perceived latency |
| Aggregate tok/s at 1/4/8/16 parallel users | What the machine delivers when shared by a team |
| **Usable users** | Highest concurrency where the median user still gets ≥ 10 tok/s and p95 TTFT ≤ 5 s |
| **$/1M output tokens** (spot and on-demand) | Live Azure price ÷ throughput — the number to compare with API pricing |

Everything uses **llama.cpp + GGUF (mostly Q4_K_M)**, so CPU and GPU machines run
the exact same model files and results are comparable. The llama.cpp build and
the GPU/driver are recorded with each run.

## Cost controls

- **Spot VMs** by default (60–90 % cheaper; e.g. T4 ≈ $0.16/h, A100 80 GB ≈ $0.94/h in
  `swedencentral` at time of writing). Eviction loses at most the model in progress,
  because results are pushed after every model.
- **Self-delete** on success: the VM's managed identity has Contributor on *its own
  resource group only* and deletes it (VM, disk, IP, everything).
- **Kill switch**: a systemd timer deletes the resource group after `max_hours`
  whatever happens. A failed run stays up `GRACE_MINUTES` (20) for SSH debugging.
- **Safety net**: `infra/cleanup.sh` lists all benchmark resource groups;
  `infra/cleanup.sh --delete` removes them.
- Recommended once: a budget alert on the subscription
  (Portal → Cost Management → Budgets, e.g. $20/month with email alerts at 50/90 %).

## One-time setup

1. **Push this repo to GitHub** (private is fine).
2. **Create a fine-grained GitHub token**: Settings → Developer settings → Fine-grained
   tokens → only this repository → *Contents: Read and write*, short expiry.
   It is passed to the VM via cloud-init and the VM is deleted after the run.
3. **Azure CLI**: install, then `az login` and `az account set -s <subscription>`.
4. **Quota** (new subscriptions usually have 0 GPU quota; free-trial subscriptions
   cannot get GPUs at all). Check and request in Portal → Quotas → Compute:
   ```bash
   az vm list-usage -l swedencentral -o table | grep -Ei 'spot|lowpriority|NCAS|NCADS|DSv5|DASv5'
   ```
   Spot VMs consume the **"Total Regional Spot vCPUs"** quota; e.g. a T4 needs 4, an A100 24.

## Running

```bash
# 0. free local smoke test (needs Docker only)
python3 bench/run.py --machine local --tags smoke --concurrency 1 2

# 1. on Azure
export GITHUB_TOKEN=github_pat_...
infra/launch.sh cpu-d8s-v5 --follow     # cheapest: ~$0.08/h spot
infra/launch.sh gpu-t4     --follow

# 2. results
git pull && python3 bench/report.py     # writes results/SUMMARY.md
```

Machines live in `config/machines.yaml`, models in `config/models.yaml`. A model is
skipped automatically if it doesn't fit in VRAM (GPU) or RAM (CPU).

Results layout:
```
results/<machine>/<UTC timestamp>/
  system.json            CPU, RAM, GPU, driver, VM size, region, live prices, image
  <model>/result.json    llama-bench + load test results, status, timings
  <model>/loadtest.json
  <model>/server.log
  <model>/usecases/<id>/   response.md, reasoning.md, result.json, extracted/  (see Use-cases)
  vm.log
```

## Use-cases

Besides speed, each model answers real tasks once (single shot, temperature 0) and the
answer is **recorded, not graded**, so you can compare models and hardware afterwards.

**Add one**: copy `usecases/c-fix-linked-list/` and edit `usecase.yaml` (`id`, `description`,
`tags`, `files` = names under `files/` to inline, `prompt` with a `{files}` placeholder,
optional `max_tokens`, default 4096). Only use-cases whose tags match `usecases.tags`
(default `[default]`, or `--usecase-tags`) run.

**Gating**: a use-case phase runs after a model's load test, only if the speed benchmark
succeeded and llama-bench tg128 ≥ `usecases.min_tg_tok_s` (default 3; override with
`--usecase-min-tok-s`). The server is restarted with `-np 1 -c 32768` (`usecases.ctx`) since
prompts contain source files. `result.json` gets `usecases_status`: `ok`, `skipped: ...`,
`disabled` (`--no-usecases` or `usecases.enabled: false`) or `error: ...`. One failing
use-case never stops the others. Config lives under `usecases:` in `config/machines.yaml`
(defaults, overridable per machine, merged key by key).

**Outputs**: `results/<machine>/<run>/<model>/usecases/<id>/` holds `response.md`,
`reasoning.md` (only if the model produced reasoning), `result.json` (finish reason
`stop`/`length`, tokens, TTFT, tok/s, total time, sha256 of the use-case so later edits are
detectable) and `extracted/<file>` (code blocks whose first line is `// file: name`,
`# file: name` or `/* file: name */`; best effort, handy to diff or compile by hand).
`python3 bench/report_usecases.py` scans all runs and writes `results/usecases.csv` and
`results/USECASES.md` (model × machine table per use-case, linking to each answer).

**Later (optional)**: a `check:` field (a compile/test command run in a sandboxed container
on `extracted/`) could add automatic pass/fail without changing this data layout.

## Suggested order (cheapest first)

1. Local smoke test — validates everything for $0.
2. `cpu-d8s-v5` with `small` models — first real run, a few cents.
3. `gpu-t4` — cheapest GPU, 8–12B models.
4. `gpu-a100` / `gpu-h100` — only once everything works; this is where the 70B and
   high-concurrency numbers come from. Keep `max_hours` tight.

## Known limits / next steps

- One engine (llama.cpp) for comparability. vLLM/SGLang will show higher aggregate
  throughput on big GPUs at high concurrency; adding them is a second runner that
  reuses `bench/loadtest.py` (it speaks the OpenAI API).
- Quality is only recorded (use-cases, no grading). A small `lm-evaluation-harness` pass against
  the same llama-server endpoint, or the `check:` idea above, would add an automatic score.
- The `ubuntu-hpc` GPU image is assumed to ship working NVIDIA drivers; NVads A10 v5
  sizes (GRID driver) are not supported.
- Pin `LLAMACPP_TAG_CPU` / `LLAMACPP_TAG_GPU` (e.g. `server-b12004`) when you want
  runs on different days to be strictly comparable.

## Contributing: commit messages

Commits follow [Conventional Commits](https://www.conventionalcommits.org) so the changelog can be
generated with [git-cliff](https://git-cliff.org) (config: `cliff.toml`):

```
<type>(<scope>): <description>      types: feat fix perf refactor docs test build ci chore style revert
feat(usecases): add rust compile-error use-case
fix(infra): retry self-destruct when the role assignment is not ready
```

- Enable the checking hook once per clone: `git config core.hooksPath .githooks`
- Generate the changelog: `git cliff -o CHANGELOG.md`; release: `git tag v0.2.0 && git cliff -o CHANGELOG.md`
- Benchmark VMs commit results as `chore(results): ...`; those are excluded from the changelog.
