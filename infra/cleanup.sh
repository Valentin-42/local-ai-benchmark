#!/usr/bin/env bash
# Safety net: list (and with --delete, remove) every benchmark resource group.
# Run it whenever in doubt; a forgotten GPU VM is the main way to lose money.
set -euo pipefail
az group list --tag purpose=llm-bench --query "[].{name:name, machine:tags.machine, expires:tags.expires}" -o table
if [ "${1:-}" = "--delete" ]; then
  for rg in $(az group list --tag purpose=llm-bench --query "[].name" -o tsv); do
    echo "deleting $rg"; az group delete -n "$rg" --yes --no-wait
  done
fi
