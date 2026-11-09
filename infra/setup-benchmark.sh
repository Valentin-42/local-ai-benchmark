#!/usr/bin/env bash
# Install benchmark prerequisites on supported Ubuntu VMs. Run as root.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "error: run this setup script as root (for example, with sudo)" >&2
  exit 1
fi

if [ ! -r /etc/os-release ]; then
  echo "error: cannot identify operating system" >&2
  exit 1
fi
# shellcheck disable=SC1091
. /etc/os-release
if [ "${ID:-}" != ubuntu ] || [[ "${VERSION_ID:-}" != 22.04 && "${VERSION_ID:-}" != 24.04 ]]; then
  echo "error: supported platforms are Ubuntu 22.04 and 24.04" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q ca-certificates curl git gpg pciutils python3-yaml docker.io
systemctl enable --now docker

if lspci | grep -qi nvidia; then
  nvidia-smi >/dev/null || {
    echo "error: NVIDIA GPU detected, but nvidia-smi failed; install/fix the host GPU driver first" >&2
    exit 1
  }
  if ! docker info 2>/dev/null | grep -qi 'runtimes:.*nvidia'; then
    echo "installing NVIDIA Container Toolkit"
    install -d -m 0755 /usr/share/keyrings
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
      | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' \
      > /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt-get update -q
    apt-get install -y -q nvidia-container-toolkit
    nvidia-ctk runtime configure --runtime=docker
    systemctl restart docker
  fi
fi
