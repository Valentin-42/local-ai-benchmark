#!/usr/bin/env bash
# Runs on the VM (as root, from cloud-init): prepare Docker/GPU, run the
# benchmark, push results, then delete the VM's resource group.
set -euo pipefail
set -a; . /etc/bench.env; set +a
cd /opt/bench
log() { echo "[$(date +%H:%M:%S)] $*"; }

log "bootstrap on $(hostname) machine=$MACHINE"

if ! command -v docker >/dev/null; then
  log "installing docker"
  apt-get install -y -q docker.io >/dev/null
fi

if lspci | grep -qi nvidia; then
  nvidia-smi >/dev/null || { log "GPU present but nvidia-smi fails (use the GPU image)"; exit 1; }
  if ! docker info 2>/dev/null | grep -qi 'runtimes:.*nvidia'; then
    log "installing nvidia-container-toolkit"
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
      | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' \
      > /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt-get update -q >/dev/null && apt-get install -y -q nvidia-container-toolkit >/dev/null
    nvidia-ctk runtime configure --runtime=docker && systemctl restart docker
  fi
fi

# Put models on the largest local disk (the temp disk /mnt when the size has one).
export MODELS_DIR=/var/lib/bench-models
if mountpoint -q /mnt && [ "$(df --output=avail -B1G /mnt | tail -1)" -gt 60 ]; then
  MODELS_DIR=/mnt/bench-models
fi
mkdir -p "$MODELS_DIR"
export DELETE_MODELS_AFTER_USE=1

git config user.name "llm-bench ($MACHINE)"
git config user.email "llm-bench@users.noreply.github.com"

status=0
python3 bench/run.py --machine "$MACHINE" --push ${DRY_RUN:+--dry-run} || status=$?

# Attach the VM log to the run for debugging.
run_dir=$(ls -d results/"$MACHINE"/*/ 2>/dev/null | sort | tail -1 || true)
if [ -n "$run_dir" ]; then
  sed "s/$GITHUB_TOKEN/***/g" /var/log/bench.log > "$run_dir/vm.log"
  git add "$run_dir" && git commit -qm "chore(results): $MACHINE vm log" && { git pull -q --rebase; git push -q; } || true
fi

if [ "$status" -eq 0 ]; then
  log "success, self-destructing"
  /usr/local/bin/bench-selfdestruct
else
  log "benchmark failed (exit $status); keeping VM ${GRACE_MINUTES} min for debugging"
  systemd-run --unit=bench-grace --on-active="${GRACE_MINUTES}m" /usr/local/bin/bench-selfdestruct
fi
