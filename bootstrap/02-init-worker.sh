#!/usr/bin/env bash
# Bootstrap script 2/3 — Join a worker node to an existing k3s cluster.
# Run as root (or with sudo) on each additional node.
#
# Usage:
#   sudo K3S_URL=https://<PRIMARY_IP>:6443 \
#        K3S_TOKEN=<token> \
#        ./bootstrap/02-init-worker.sh [TIER]
#
# TIER controls the uns.fde/tier label:
#   worker    — general stateless workloads (default)
#   stateful  — StatefulSets: MonsterMQ, Neo4j, TimeBase historian
#   ot-edge   — OT hardware access: Ignition, LitmusEdge (needs privileged pods)

set -euo pipefail

TIER="${1:-worker}"

if [[ -z "${K3S_URL:-}" ]] || [[ -z "${K3S_TOKEN:-}" ]]; then
  echo "ERROR: K3S_URL and K3S_TOKEN must be set as environment variables."
  echo "  Example:"
  echo "    sudo K3S_URL=https://192.168.1.10:6443 K3S_TOKEN=xxx ./02-init-worker.sh ot-edge"
  exit 1
fi

echo "==> Joining cluster at ${K3S_URL} as tier=${TIER}"

curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="\
  agent \
  --server ${K3S_URL} \
  --token ${K3S_TOKEN} \
  --node-label uns.fde/tier=${TIER} \
" sh -

echo "==> Worker joined. Label: uns.fde/tier=${TIER}"
echo "    Verify on control-plane: kubectl get nodes -L uns.fde/tier"
