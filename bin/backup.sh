#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
#  backup.sh — Backup FDE stack data
#
#  Usage: ./backup.sh [--out <path>]
#         ./backup.sh --help
#
#  Backs up:
#    - ClickHouse (BACKUP ALL, or CSV export fallback)
#    - Qdrant     (collection snapshots via REST API)
#    - Neo4j       (neo4j-admin database dump)
#    - Helm values for all uns releases
#    - Full Kubernetes state (all,pvc,configmap,secret,ingress)
# ════════════════════════════════════════════════════════════════════════════
set -euo pipefail

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
DEFAULT_OUT="/home/nandor/backups/fde-backup-${TIMESTAMP}.tar.gz"
OUT=""

# ── Colours ───────────────────────────────────────────────────────────────────
BOLD=$'\e[1m'; RESET=$'\e[0m'
GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RED=$'\e[31m'; CYAN=$'\e[36m'

# ── Usage ─────────────────────────────────────────────────────────────────────
usage() {
  cat <<EOF
Usage: $0 [--out <path>]

Backup FDE stack data to a compressed archive.

Options:
  --out <path>   Output archive path
                 (default: $DEFAULT_OUT)
  --help         Show this help and exit
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)    OUT="$2"; shift 2 ;;
    --help|-h) usage ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
done

[[ -z "$OUT" ]] && OUT="$DEFAULT_OUT"

# ── Logging ───────────────────────────────────────────────────────────────────
phase() { echo -e "\n${BOLD}${CYAN}════ $* ${RESET}"; }
info()  { echo -e "  ${BOLD}→${RESET}  $*"; }
ok()    { echo -e "  ${GREEN}✔${RESET}  $*"; }
warn()  { echo -e "  ${YELLOW}!${RESET}  $*"; }
die()   { echo -e "  ${RED}✘${RESET}  $*" >&2; exit 1; }

# ── Cleanup tracking ──────────────────────────────────────────────────────────
TMPDIR=""
BGPIDS=()
cleanup() {
  for pid in "${BGPIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  [[ -n "$TMPDIR" ]] && rm -rf "$TMPDIR"
}
trap cleanup EXIT

# ── Helper: find first pod matching a Helm release ───────────────────────────
find_pod() {
  local release="$1" ns="${2:-uns}"
  local pod
  # 1. Standard instance label
  pod=$(kubectl get pods -n "$ns" \
    -l "app.kubernetes.io/instance=${release}" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  [[ -n "$pod" ]] && { echo "$pod"; return 0; }
  # 2. Fallback: name prefix
  pod=$(kubectl get pods -n "$ns" --no-headers \
    -o custom-columns=":metadata.name" 2>/dev/null \
    | grep "^${release}" | head -1 || true)
  [[ -n "$pod" ]] && { echo "$pod"; return 0; }
  echo ""
}

# ── 1. Setup ──────────────────────────────────────────────────────────────────
phase "1: Setup"
TMPDIR="$(mktemp -d /tmp/fde-backup-XXXXXX)"
mkdir -p "$(dirname "$OUT")"
info "Work dir : $TMPDIR"
info "Output   : $OUT"

# ── 2. ClickHouse backup ──────────────────────────────────────────────────────
phase "2: ClickHouse"
CH_POD=$(find_pod fde-clickhouse)
if [[ -z "$CH_POD" ]]; then
  warn "fde-clickhouse pod not found — skipping"
else
  info "Pod: $CH_POD"
  mkdir -p "$TMPDIR/clickhouse"
  # Try native BACKUP ALL (requires 'backups' disk configured in ClickHouse)
  if kubectl exec -n uns "$CH_POD" -- \
      clickhouse-client --query "BACKUP ALL TO Disk('backups', 'fde-backup')" \
      2>/dev/null; then
    ok "BACKUP ALL succeeded"
    # Copy backup files out of pod
    kubectl exec -n uns "$CH_POD" -- \
      find /var/lib/clickhouse/disks/backups/fde-backup -type f 2>/dev/null \
      | while read -r remote_file; do
          local_file="$TMPDIR/clickhouse/$(basename "$remote_file")"
          kubectl cp "uns/$CH_POD:$remote_file" "$local_file" 2>/dev/null || true
        done
    ok "Backup files copied"
  else
    warn "BACKUP ALL unavailable — falling back to CSV export"
    # Get list of user tables
    kubectl exec -n uns "$CH_POD" -- clickhouse-client \
      --query "SELECT database, name FROM system.tables
               WHERE database NOT IN ('system','information_schema','INFORMATION_SCHEMA')" \
      --format TabSeparated 2>/dev/null \
      > "$TMPDIR/clickhouse/tables.tsv" || {
        warn "Could not list tables — ClickHouse may be unavailable"; true
    }
    # Export each table as CSVWithNames
    while IFS=$'\t' read -r db tbl; do
      [[ -z "$db" || -z "$tbl" ]] && continue
      info "Exporting ${db}.${tbl}…"
      kubectl exec -n uns "$CH_POD" -- \
        clickhouse-client \
          --query "SELECT * FROM ${db}.${tbl} FORMAT CSVWithNames" \
        2>/dev/null \
        > "$TMPDIR/clickhouse/${db}__${tbl}.csv" \
        && ok "Exported ${db}.${tbl}" \
        || warn "Failed to export ${db}.${tbl}"
    done < "$TMPDIR/clickhouse/tables.tsv"
  fi
fi

# ── 3. Qdrant snapshots ───────────────────────────────────────────────────────
phase "3: Qdrant"
QDRANT_POD=$(find_pod fde-qdrant)
if [[ -z "$QDRANT_POD" ]]; then
  warn "fde-qdrant pod not found — skipping"
else
  info "Pod: $QDRANT_POD"
  mkdir -p "$TMPDIR/qdrant"
  LOCAL_PORT=16333
  kubectl port-forward -n uns "$QDRANT_POD" "${LOCAL_PORT}:6333" &>/dev/null &
  BGPIDS+=($!)
  sleep 3
  # Discover collections
  COLLECTIONS=$(curl -s "http://localhost:${LOCAL_PORT}/collections" \
    | python3 -c \
      "import sys,json; [print(c['name']) for c in json.load(sys.stdin)['result']['collections']]" \
      2>/dev/null || true)
  if [[ -z "$COLLECTIONS" ]]; then
    warn "No collections found or Qdrant API unreachable"
  else
    while IFS= read -r coll; do
      [[ -z "$coll" ]] && continue
      info "Snapshotting: $coll"
      SNAP_NAME=$(curl -s -X POST \
        "http://localhost:${LOCAL_PORT}/collections/${coll}/snapshots" \
        | python3 -c \
          "import sys,json; r=json.load(sys.stdin); print(r['result']['name'])" \
          2>/dev/null || true)
      if [[ -n "$SNAP_NAME" ]]; then
        curl -s -o "$TMPDIR/qdrant/${coll}---${SNAP_NAME}" \
          "http://localhost:${LOCAL_PORT}/collections/${coll}/snapshots/${SNAP_NAME}" \
          && ok "Snapshot saved: $coll" \
          || warn "Failed to download snapshot: $coll"
      else
        warn "Could not create snapshot for: $coll"
      fi
    done <<< "$COLLECTIONS"
  fi
  # Stop port-forward
  kill "${BGPIDS[-1]}" 2>/dev/null || true
  unset 'BGPIDS[-1]'
fi

# ── 4. Neo4j dump ────────────────────────────────────────────────────────────
phase "4: Neo4j"
NEO_POD=$(kubectl get pods -n uns -l app.kubernetes.io/instance=fde-neo4j \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [[ -z "$NEO_POD" ]]; then
  warn "fde-neo4j pod not found — skipping"
else
  info "Pod: $NEO_POD"
  mkdir -p "$TMPDIR/neo4j"
  kubectl exec -n uns "$NEO_POD" -- \
    neo4j-admin database dump neo4j --to-path=/tmp/neo4j-dump 2>/dev/null \
    && kubectl cp "uns/$NEO_POD:/tmp/neo4j-dump/neo4j.dump" "$TMPDIR/neo4j/neo4j.dump" \
    && ok "neo4j-admin dump complete" \
    || warn "neo4j-admin dump failed — check pod logs"
fi

# ── 5. Helm values snapshot ───────────────────────────────────────────────────
phase "5: Helm values"
mkdir -p "$TMPDIR/helm-values"
for release in fde-nats fde-neo4j fde-clickhouse fde-qdrant fde-pgadmin \
               fde-maestrohub fde-ignition fde-monitoring fde-loki predmaint; do
  if helm status "$release" -n uns &>/dev/null; then
    helm get values "$release" -n uns \
      > "$TMPDIR/helm-values/values-${release}.yaml" \
      && ok "Values saved: $release" \
      || warn "Could not get values: $release"
  fi
done

# ── 6. Kubernetes state ───────────────────────────────────────────────────────
phase "6: Kubernetes state"
kubectl get all,pvc,configmap,secret,ingress,servicemonitor \
  -n uns -o yaml \
  > "$TMPDIR/k8s-state.yaml" 2>/dev/null \
  && ok "k8s-state.yaml saved" \
  || warn "kubectl get state failed"

# ── 7. Create archive ─────────────────────────────────────────────────────────
phase "7: Archive"
tar -czf "$OUT" -C "$TMPDIR" .
ok "Archive created: $OUT"
ls -lh "$OUT"

echo ""
echo "════ Backup complete: $OUT ══════════════════════════════"
echo ""
