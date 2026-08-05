#!/usr/bin/env bash
# Bootstrap script 1/3 — Install k3s control-plane on the PRIMARY node.
# Run as root (or with sudo) on the node that will be the cluster master.
#
# Usage:
#   sudo ./bootstrap/01-init-cluster.sh [NODE_EXTERNAL_IP]
#
# After this completes, note the K3S_TOKEN printed at the end and
# run 02-init-worker.sh on each additional node.

set -euo pipefail

PRIMARY_IP="${1:-$(hostname -I | awk '{print $1}')}"

echo "==> Installing k3s control-plane on ${PRIMARY_IP}"

# Install k3s with embedded etcd (production-grade), no traefik (we use NGINX)
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="\
  server \
  --cluster-init \
  --tls-san ${PRIMARY_IP} \
  --disable traefik \
  --node-label uns.fde/tier=stateful \
  --write-kubeconfig-mode 644 \
" sh -

echo "==> Waiting for k3s to be ready..."
until kubectl get nodes 2>/dev/null | grep -q "Ready"; do sleep 3; done

echo "==> Labelling control-plane node"
NODE_NAME=$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')
kubectl label node "${NODE_NAME}" uns.fde/tier=stateful --overwrite

echo "==> Installing Helm"
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

echo "==> Installing NGINX Ingress Controller"
helm upgrade --install ingress-nginx ingress-nginx \
  --repo https://kubernetes.github.io/ingress-nginx \
  --namespace ingress-nginx --create-namespace \
  --set controller.hostNetwork=true \
  --set controller.kind=DaemonSet \
  --wait

echo "==> Installing cert-manager (optional TLS)"
helm upgrade --install cert-manager cert-manager \
  --repo https://charts.jetstack.io \
  --namespace cert-manager --create-namespace \
  --set installCRDs=true \
  --wait

echo ""
echo "================================================================"
echo " Control-plane ready."
echo " Kubeconfig: /etc/rancher/k3s/k3s.yaml"
echo " Copy to ~/.kube/config or export KUBECONFIG=/etc/rancher/k3s/k3s.yaml"
echo ""
echo " Worker join token:"
cat /var/lib/rancher/k3s/server/node-token
echo ""
echo " Run on worker nodes:"
echo "   sudo K3S_URL=https://${PRIMARY_IP}:6443 \\"
echo "        K3S_TOKEN=<token-above> \\"
echo "        ./bootstrap/02-init-worker.sh [WORKER_TIER]"
echo "================================================================"
