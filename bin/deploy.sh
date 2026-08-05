#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
#  deploy.sh — Deploy the FDE (Factory Digital Edge) K8s stack
#
#  Usage: ./deploy.sh [--host <ip>] [--values <file>] [--chart-dir <dir>]
#         ./deploy.sh --help
#
#  Release names discovered 2026-07-23 via helm list -A on nandor:
#    uns ns  : fde-nats, fde-age, fde-clickhouse, fde-qdrant, fde-pgadmin,
#              fde-maestrohub, fde-ignition, fde-monitoring, fde-loki, predmaint
#    infra   : ingress-nginx (ingress-nginx ns), cert-manager (cert-manager ns)
# ════════════════════════════════════════════════════════════════════════════
set -euo pipefail

CHART_DIR="${CHART_DIR:-/home/nandor/fde-k8s}"
EXTRA_VALUES=""
HOST=""

# ── Colours ───────────────────────────────────────────────────────────────────
BOLD=$'\e[1m'; RESET=$'\e[0m'
GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RED=$'\e[31m'; CYAN=$'\e[36m'

# ── Usage ─────────────────────────────────────────────────────────────────────
usage() {
  cat <<EOF
Usage: $0 [OPTIONS]

Deploy the FDE (Factory Digital Edge) stack to Kubernetes.

Options:
  --host <ip>         Target node IP (used for access URL output)
  --values <file>     Extra Helm values file applied to all FDE app releases
  --chart-dir <dir>   Base directory containing charts/ and values-*.yaml
                      (default: /home/nandor/fde-k8s)
  --help              Show this help and exit

Environment:
  CHART_DIR           Same as --chart-dir
  KUBECONFIG          Kubernetes config file

Examples:
  ./deploy.sh
  ./deploy.sh --host 192.168.100.12 --values site-overrides.yaml
EOF
  exit 0
}

# ── Arg parsing ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)       HOST="$2";         shift 2 ;;
    --values)     EXTRA_VALUES="$2"; shift 2 ;;
    --chart-dir)  CHART_DIR="$2";    shift 2 ;;
    --help|-h)    usage ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
done

[[ -n "$EXTRA_VALUES" && ! -f "$EXTRA_VALUES" ]] && {
  echo "${RED}ERROR${RESET}: values file not found: $EXTRA_VALUES" >&2; exit 1
}

# ── Logging ───────────────────────────────────────────────────────────────────
phase() { echo -e "\n${BOLD}${CYAN}════ $* ${RESET}"; }
info()  { echo -e "  ${BOLD}→${RESET}  $*"; }
ok()    { echo -e "  ${GREEN}✔${RESET}  $*"; }
warn()  { echo -e "  ${YELLOW}!${RESET}  $*"; }
die()   { echo -e "  ${RED}✘${RESET}  $*" >&2; exit 1; }

# ── Helper: helm upgrade --install with optional values ───────────────────────
helm_up() {
  # helm_up <release> <chart-path-or-repo-ref> <namespace> [extra-helm-args...]
  local release="$1" chart="$2" ns="$3"; shift 3
  local -a cmd=(helm upgrade --install "$release" "$chart"
                --namespace "$ns" --create-namespace "$@")
  [[ -n "$EXTRA_VALUES" ]] && cmd+=(--values "$EXTRA_VALUES")
  info "helm upgrade --install $release  chart=$chart  ns=$ns"
  "${cmd[@]}"
  ok "$release installed"
}

# ── 1. Namespace ──────────────────────────────────────────────────────────────
phase "1: Namespace"
kubectl create namespace uns --dry-run=client -o yaml | kubectl apply -f -
ok "Namespace uns ensured"

# ── 2-3. Helm repos ───────────────────────────────────────────────────────────
phase "2-3: Helm repos"
helm repo add ingress-nginx        https://kubernetes.github.io/ingress-nginx        --force-update
helm repo add jetstack             https://charts.jetstack.io                        --force-update
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
helm repo add grafana              https://grafana.github.io/helm-charts             --force-update
helm repo update
ok "Repos updated"

# ── 4. ingress-nginx ──────────────────────────────────────────────────────────
phase "4: ingress-nginx  (ns=ingress-nginx)"
helm_up ingress-nginx ingress-nginx/ingress-nginx ingress-nginx \
  --set controller.hostNetwork=true \
  --set controller.hostPort.enabled=true \
  --set controller.kind=DaemonSet

# ── 5-6. cert-manager ─────────────────────────────────────────────────────────
phase "5-6: cert-manager  (ns=cert-manager)"
helm_up cert-manager jetstack/cert-manager cert-manager \
  --set installCRDs=true
info "Waiting for cert-manager rollouts…"
kubectl rollout status deployment cert-manager            -n cert-manager --timeout=120s
kubectl rollout status deployment cert-manager-webhook    -n cert-manager --timeout=120s
kubectl rollout status deployment cert-manager-cainjector -n cert-manager --timeout=120s
ok "cert-manager ready"

# ── 7. ClusterIssuer ──────────────────────────────────────────────────────────
phase "7: ClusterIssuer"
CI_YAML="$CHART_DIR/observability/clusterissuer-selfsigned.yaml"
if [[ -f "$CI_YAML" ]]; then
  kubectl apply -f "$CI_YAML"
  ok "ClusterIssuer applied: $CI_YAML"
else
  warn "Skipped — not found: $CI_YAML"
fi

# ── 8. fde-monitoring (Prometheus + Grafana)  ─────────────────────────────────
phase "8: fde-monitoring  (kube-prometheus-stack equivalent)"
MON_CHART="$CHART_DIR/charts/monitoring"
[[ -d "$MON_CHART" ]] || die "Chart directory not found: $MON_CHART"
MON_ARGS=()
[[ -f "$CHART_DIR/values-monitoring.yaml" ]] && MON_ARGS+=(--values "$CHART_DIR/values-monitoring.yaml")
helm_up fde-monitoring "$MON_CHART" uns "${MON_ARGS[@]}"

# ── 9. fde-loki ───────────────────────────────────────────────────────────────
phase "9: fde-loki"
LOKI_CHART="$CHART_DIR/charts/loki"
[[ -d "$LOKI_CHART" ]] || die "Chart directory not found: $LOKI_CHART"
helm_up fde-loki "$LOKI_CHART" uns

# ── 10. FDE application releases ─────────────────────────────────────────────
phase "10: FDE application releases  (ns=uns)"
# Maps: Helm release name → chart subdirectory under $CHART_DIR/charts/
declare -A CHART_MAP=(
  [fde-nats]=nats
  [fde-age]=apache-age
  [fde-clickhouse]=clickhouse
  [fde-qdrant]=qdrant
  [fde-pgadmin]=pgadmin
  [fde-maestrohub]=maestrohub
  [fde-ignition]=ignition
  [predmaint]=predmaint
)
# Optional per-release values files (relative to CHART_DIR)
declare -A VALUES_MAP=(
  [fde-age]="$CHART_DIR/values-age.yaml"
  [fde-clickhouse]="$CHART_DIR/values-clickhouse.yaml"
  [fde-qdrant]="$CHART_DIR/values-qdrant.yaml"
)

# Deployment order: messaging first, then storage, then apps
RELEASE_ORDER=(fde-nats fde-age fde-clickhouse fde-qdrant fde-pgadmin fde-maestrohub fde-ignition predmaint)

for release in "${RELEASE_ORDER[@]}"; do
  chart_path="$CHART_DIR/charts/${CHART_MAP[$release]}"
  if [[ ! -d "$chart_path" ]]; then
    warn "Chart dir missing: $chart_path  — skipping $release"
    continue
  fi
  install_args=()
  vfile="${VALUES_MAP[$release]:-}"
  [[ -n "$vfile" && -f "$vfile" ]] && install_args+=(--values "$vfile")
  helm_up "$release" "$chart_path" uns "${install_args[@]}"
done

# ── 11. Access URLs ───────────────────────────────────────────────────────────
phase "Deployment complete"
NODE_IP="${HOST:-$(kubectl get nodes \
  -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' \
  2>/dev/null || echo '<node-ip>')}"

cat <<EOF

  Access URLs (point /etc/hosts entries to $NODE_IP):

    pgAdmin UI           http://pgadmin.fde.local
    MaestroHub (n8n)     http://maestro.fde.local
    Ignition SCADA       http://ignition.fde.local
    Grafana              http://grafana.fde.local
    ClickHouse HTTP      http://$NODE_IP:8123
    Qdrant REST          http://$NODE_IP:6333
    NATS / MQTT bridge   $NODE_IP:31883

  Quick status:
    kubectl get pods -n uns
    helm list -A

EOF
