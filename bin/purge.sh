#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
#  purge.sh — Completely tear down the FDE K8s stack
#
#  Usage: ./purge.sh --yes-i-really-mean-it [--keep-data]
#         ./purge.sh --help
#
#  WARNING: Destructive and irreversible without a prior backup!
# ════════════════════════════════════════════════════════════════════════════
set -euo pipefail

CONFIRMED=false
KEEP_DATA=false

# ── Colours ───────────────────────────────────────────────────────────────────
BOLD=$'\e[1m'; RESET=$'\e[0m'
GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RED=$'\e[31m'; CYAN=$'\e[36m'

# ── Usage ─────────────────────────────────────────────────────────────────────
usage() {
  cat <<EOF
Usage: $0 --yes-i-really-mean-it [--keep-data]

Completely uninstall the FDE stack from Kubernetes.

Options:
  --yes-i-really-mean-it   Required safety flag — script aborts without it
  --keep-data              Skip PVC and namespace deletion (preserves data volumes)
  --help                   Show this help and exit

Without --keep-data, ALL persistent volumes will be permanently deleted.
Take a backup first: ./backup.sh
EOF
  exit 0
}

# ── Arg parsing ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes-i-really-mean-it) CONFIRMED=true; shift ;;
    --keep-data)            KEEP_DATA=true; shift ;;
    --help|-h)              usage ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
done

$CONFIRMED || {
  echo ""
  echo "${RED}${BOLD}ERROR${RESET}: Safety flag required."
  echo ""
  echo "  This script permanently deletes all FDE data."
  echo "  Re-run with:  $0 --yes-i-really-mean-it"
  echo "  Add:          --keep-data   to preserve PVCs"
  echo ""
  exit 1
}

# ── Logging ───────────────────────────────────────────────────────────────────
phase() { echo -e "\n${BOLD}${CYAN}════ $* ${RESET}"; }
info()  { echo -e "  ${BOLD}→${RESET}  $*"; }
ok()    { echo -e "  ${GREEN}✔${RESET}  $*"; }
warn()  { echo -e "  ${YELLOW}!${RESET}  $*"; }

# ── Warning + countdown ───────────────────────────────────────────────────────
echo ""
echo "${RED}${BOLD}╔══════════════════════════════════════════════════════════╗${RESET}"
echo "${RED}${BOLD}║   WARNING: PURGING THE ENTIRE FDE STACK                  ║${RESET}"
if $KEEP_DATA; then
echo "${YELLOW}${BOLD}║   Mode: --keep-data  (PVCs and namespaces preserved)     ║${RESET}"
else
echo "${RED}${BOLD}║   Mode: FULL PURGE — all PVCs + namespaces DELETED        ║${RESET}"
fi
echo "${RED}${BOLD}╚══════════════════════════════════════════════════════════╝${RESET}"
echo ""
echo "  Ctrl-C to abort. Proceeding in:"
for i in 10 9 8 7 6 5 4 3 2 1; do
  printf "    %2d...\r" "$i"
  sleep 1
done
echo ""

# ── Helper: uninstall if installed, skip otherwise ───────────────────────────
helm_del() {
  local release="$1" ns="$2"
  if helm status "$release" -n "$ns" &>/dev/null; then
    info "Uninstalling $release  (ns=$ns)"
    helm uninstall "$release" -n "$ns"
    ok "$release removed"
  else
    warn "Skipped (not installed): $release"
  fi
}

# ── 1. FDE application releases (uns) ────────────────────────────────────────
phase "1: FDE application releases  (ns=uns)"
# Release names discovered 2026-07-23 via helm list -A
for release in predmaint fde-ignition fde-maestrohub fde-pgadmin fde-qdrant fde-clickhouse fde-age fde-nats; do
  helm_del "$release" uns
done

# ── 2. Observability releases (uns) ──────────────────────────────────────────
phase "2: Observability releases  (ns=uns)"
helm_del fde-loki       uns
helm_del fde-monitoring uns

# ── 3. PVCs (unless --keep-data) ─────────────────────────────────────────────
if ! $KEEP_DATA; then
  phase "3: PVCs  (ns=uns)"
  info "Deleting all PVCs in uns…"
  kubectl delete pvc --all -n uns --wait --ignore-not-found
  ok "PVCs deleted"
else
  warn "Phase 3 skipped: --keep-data"
fi

# ── 4. Namespaces (unless --keep-data) ───────────────────────────────────────
if ! $KEEP_DATA; then
  phase "4: Namespaces"
  kubectl delete namespace uns ingress-nginx cert-manager \
    --ignore-not-found --wait
  ok "Namespaces deleted"
else
  warn "Phase 4 skipped: --keep-data"
fi

# ── 5. Infrastructure releases ───────────────────────────────────────────────
phase "5: Infrastructure releases"
helm_del ingress-nginx ingress-nginx
helm_del cert-manager  cert-manager

echo ""
echo "════ Purge complete. ═══════════════════════════════════════════════════"
echo ""
