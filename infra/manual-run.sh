#!/usr/bin/env bash
# Prepare an already provisioned VM and run a benchmark without pushing results.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

MACHINE=${1:-}
if [ -z "$MACHINE" ] || [ "$#" -ne 1 ]; then
  echo "usage: infra/manual-run.sh <machine>" >&2
  exit 2
fi
if [ "$(id -u)" -eq 0 ]; then
  echo "error: run this script as your regular VM user, not root" >&2
  exit 1
fi

sudo bash "$ROOT/infra/setup-benchmark.sh"

if ! python3 - "$MACHINE" "$ROOT/config/machines.yaml" <<'PY'
import sys
import yaml

machine, config_path = sys.argv[1:]
config = yaml.safe_load(open(config_path, encoding="utf-8"))
machines = config.get("machines", {})
if machine not in machines:
    sys.exit(f"unknown machine {machine!r}; choose from: {', '.join(machines)}")
PY
then
  exit 2
fi

RUN_USER=$(id -un)
if docker info >/dev/null 2>&1; then
  python3 "$ROOT/bench/run.py" --machine "$MACHINE"
else
  echo "adding $RUN_USER to the docker group for this run"
  sudo usermod -aG docker "$RUN_USER"
  sudo -u "$RUN_USER" -g docker docker info >/dev/null
  env_args=()
  for name in HF_TOKEN MODELS_DIR LLAMACPP_IMAGE LLAMACPP_TAG_CPU LLAMACPP_TAG_GPU DELETE_MODELS_AFTER_USE; do
    if [[ -v $name ]]; then
      env_args+=("$name=${!name}")
    fi
  done
  sudo -u "$RUN_USER" -g docker env "${env_args[@]}" \
    python3 "$ROOT/bench/run.py" --machine "$MACHINE"
fi
