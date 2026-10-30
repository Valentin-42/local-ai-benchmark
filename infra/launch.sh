#!/usr/bin/env bash
# Launch one ephemeral benchmark VM. It clones this repo, benchmarks, pushes
# results to GitHub and deletes its own resource group.
#
# Usage:   infra/launch.sh <machine> [--follow] [--dry-run]
#          --dry-run: boot, clone, check docker/GPU, push system.json, self-destruct (no models)
# Env:     GITHUB_TOKEN  fine-grained PAT, "Contents: read & write" on this repo only (required)
#          HF_TOKEN      Hugging Face token (only for gated models)
#          REPO          owner/repo (default: parsed from `git remote get-url origin`)
#          BRANCH        branch to clone and push to (default: current branch)
#          GRACE_MINUTES how long a failed VM stays up for SSH debugging (default 20)
set -euo pipefail
cd "$(dirname "$0")/.."

MACHINE=${1:?usage: infra/launch.sh <machine> [--follow] [--dry-run]}
FOLLOW="" DRY_RUN=""
for arg in "${@:2}"; do
  case "$arg" in
    --follow) FOLLOW=--follow ;;
    --dry-run) DRY_RUN=1 ;;
    *) echo "unknown option $arg" >&2; exit 1 ;;
  esac
done
: "${GITHUB_TOKEN:?export GITHUB_TOKEN (fine-grained PAT with contents:write on this repo)}"
REPO=${REPO:-$(git remote get-url origin | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')}
BRANCH=${BRANCH:-$(git branch --show-current)}
GRACE_MINUTES=${GRACE_MINUTES:-20}

# The VM clones the remote, so local changes must be pushed first.
if [ -n "$(git status --porcelain -- bench config infra)" ] || [ "$(git rev-parse HEAD)" != "$(git rev-parse "origin/$BRANCH" 2>/dev/null)" ]; then
  echo "error: commit and push bench/ config/ infra/ changes first (the VM clones origin/$BRANCH)" >&2
  exit 1
fi

# Resolve the machine profile (defaults + overrides) from config/machines.yaml.
eval "$(python3 - "$MACHINE" <<'EOF'
import sys, shlex, yaml
cfg = yaml.safe_load(open("config/machines.yaml"))
if sys.argv[1] not in cfg["machines"]:
    sys.exit(f"unknown machine {sys.argv[1]!r}; choose from: {', '.join(cfg['machines'])}")
p = {**cfg["defaults"], **cfg["machines"][sys.argv[1]]}
image = p["gpu_image"] if p.get("gpu") else p["cpu_image"]
for k, v in [("SIZE", p["size"]), ("REGION", p["region"]), ("SPOT", str(p["spot"]).lower()),
             ("MAX_HOURS", p["max_hours"]), ("DISK_GB", p["os_disk_gb"]), ("IMAGE", image)]:
    print(f"{k}={shlex.quote(str(v))}")
EOF
)"

[ -n "$DRY_RUN" ] && MAX_HOURS=1      # a flow check needs ~10 min; keep the kill switch tight

STAMP=$(date -u +%Y%m%d-%H%M%S)
RG="llmbench-${MACHINE}-${STAMP}"
VM="bench"
CLOUD_INIT=$(mktemp)
trap 'rm -f "$CLOUD_INIT"' EXIT

sed -e "s|__REPO__|$REPO|" -e "s|__BRANCH__|$BRANCH|" -e "s|__MACHINE__|$MACHINE|" \
    -e "s|__GITHUB_TOKEN__|$GITHUB_TOKEN|" -e "s|__HF_TOKEN__|${HF_TOKEN:-}|" \
    -e "s|__MAX_HOURS__|$MAX_HOURS|" -e "s|__GRACE_MINUTES__|$GRACE_MINUTES|" -e "s|__DRY_RUN__|$DRY_RUN|" \
    infra/cloud-init.yaml.tpl > "$CLOUD_INIT"

echo "==> $MACHINE: $SIZE in $REGION (spot=$SPOT, kill switch ${MAX_HOURS}h${DRY_RUN:+, DRY RUN}) -> resource group $RG"
az group create -n "$RG" -l "$REGION" -o none \
  --tags purpose=llm-bench machine="$MACHINE" expires="$(date -u -d "+${MAX_HOURS} hours" +%FT%TZ)"
RG_ID=$(az group show -n "$RG" --query id -o tsv)

SPOT_ARGS=()
[ "$SPOT" = "true" ] && SPOT_ARGS=(--priority Spot --eviction-policy Delete --max-price -1)

if ! az vm create -g "$RG" -n "$VM" -o none \
    --size "$SIZE" --image "$IMAGE" "${SPOT_ARGS[@]}" \
    --security-type Standard \
    --os-disk-size-gb "$DISK_GB" --storage-sku StandardSSD_LRS --os-disk-delete-option Delete \
    --admin-username azureuser --generate-ssh-keys \
    --public-ip-sku Standard --nsg-rule NONE \
    --assign-identity '[system]' --role Contributor --scope "$RG_ID" \
    --custom-data "$CLOUD_INIT"; then
  echo "VM creation failed (quota? spot capacity? try another region). Deleting $RG." >&2
  az group delete -n "$RG" --yes --no-wait
  exit 1
fi

# SSH only from this machine's public IP (for debugging; the VM needs no inbound traffic).
MYIP=$(curl -s https://api.ipify.org)
az network nsg rule create -g "$RG" --nsg-name "${VM}NSG" -n ssh-from-launcher -o none \
  --priority 1000 --protocol Tcp --destination-port-ranges 22 --source-address-prefixes "$MYIP/32"
IP=$(az vm show -d -g "$RG" -n "$VM" --query publicIps -o tsv)

cat <<EOF
==> VM is booting. It will push results to $REPO@$BRANCH and delete $RG by itself.
    follow logs : ssh azureuser@$IP sudo tail -f /var/log/bench.log
    kill now    : az group delete -n $RG --yes --no-wait
EOF

if [ "$FOLLOW" = "--follow" ]; then
  echo "==> waiting for resource group to disappear (Ctrl-C is safe, the VM keeps going)"
  while az group exists -n "$RG" | grep -q true; do sleep 60; done
  echo "==> $RG deleted. git pull to get the results, then: python3 bench/report.py"
fi
