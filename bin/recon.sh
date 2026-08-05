#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
#  recon.sh — FDE cluster inventory and status report
#  Usage: ./recon.sh
# ════════════════════════════════════════════════════════════════════════════
set -euo pipefail

BOLD=$'\e[1m'; RESET=$'\e[0m'; CYAN=$'\e[36m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'

header() {
  echo ""
  echo "${BOLD}${CYAN}════════════════════════════════════════════════════════${RESET}"
  echo "${BOLD}${CYAN}  $*${RESET}"
  echo "${CYAN}════════════════════════════════════════════════════════${RESET}"
}

safe_kubectl() {
  kubectl "$@" 2>/dev/null || echo "  (kubectl command failed or resource not found)"
}

safe_helm() {
  helm "$@" 2>/dev/null || echo "  (helm command failed)"
}

echo ""
echo "${BOLD}FDE Cluster Recon Report — $(date '+%Y-%m-%d %H:%M:%S')${RESET}"

# ── Nodes ─────────────────────────────────────────────────────────────────────
header "NODES"
safe_kubectl get nodes -o wide

# ── uns namespace: Pods ───────────────────────────────────────────────────────
header "PODS — ns=uns"
safe_kubectl get pods -n uns -o wide

# ── uns namespace: PVCs ───────────────────────────────────────────────────────
header "PERSISTENT VOLUME CLAIMS — ns=uns"
safe_kubectl get pvc -n uns -o wide

# ── uns namespace: Services ───────────────────────────────────────────────────
header "SERVICES — ns=uns"
safe_kubectl get svc -n uns -o wide

# ── uns namespace: Ingress ────────────────────────────────────────────────────
header "INGRESS — ns=uns"
safe_kubectl get ingress -n uns -o wide

# ── uns namespace: ServiceMonitors ───────────────────────────────────────────
header "SERVICE MONITORS — ns=uns"
kubectl get servicemonitor -n uns -o wide 2>/dev/null \
  || echo "  (no ServiceMonitors, or CRD not installed)"

# ── Helm releases ─────────────────────────────────────────────────────────────
header "HELM RELEASES — all namespaces"
safe_helm list -A

# ── Resource usage: nodes ─────────────────────────────────────────────────────
header "RESOURCE USAGE — nodes"
kubectl top nodes 2>/dev/null || echo "  (metrics-server not available)"

# ── Resource usage: uns pods ──────────────────────────────────────────────────
header "RESOURCE USAGE — uns pods"
kubectl top pods -n uns 2>/dev/null || echo "  (metrics-server not available)"

# ── Summary ───────────────────────────────────────────────────────────────────
header "SUMMARY"
NODE_COUNT=$(kubectl get nodes --no-headers 2>/dev/null | wc -l || echo "?")
POD_TOTAL=$(kubectl get pods -n uns --no-headers 2>/dev/null | wc -l || echo "?")
POD_RUN=$(kubectl get pods -n uns --no-headers 2>/dev/null | grep -c "Running" || echo "?")
POD_BAD=$(kubectl get pods -n uns --no-headers 2>/dev/null \
  | grep -cE "(Error|CrashLoop|Pending|ImagePull)" || echo "0")
PVC_COUNT=$(kubectl get pvc -n uns --no-headers 2>/dev/null | wc -l || echo "?")
HELM_UNS=$(helm list -n uns --no-headers 2>/dev/null | wc -l || echo "?")
HELM_ALL=$(helm list -A --no-headers 2>/dev/null | wc -l || echo "?")

echo "  Nodes:              $NODE_COUNT"
echo "  Pods (uns):         $POD_TOTAL total  |  $POD_RUN Running  |  $POD_BAD unhealthy"
echo "  PVCs (uns):         $PVC_COUNT"
echo "  Helm (uns):         $HELM_UNS releases"
echo "  Helm (all ns):      $HELM_ALL releases"
if [[ "$POD_BAD" -gt 0 ]]; then
  echo ""
  echo "  ${YELLOW}Unhealthy pods:${RESET}"
  kubectl get pods -n uns --no-headers 2>/dev/null \
    | grep -E "(Error|CrashLoop|Pending|ImagePull)" \
    | awk '{printf "    %-40s %s\n", $1, $3}' || true
fi
echo ""
