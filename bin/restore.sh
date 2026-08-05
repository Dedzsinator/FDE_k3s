#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
#  restore.sh — Restore FDE stack from a backup archive
#
#  Usage: ./restore.sh <backup.tar.gz>
#         ./restore.sh --help
#
#  Requires a backup archive created by backup.sh.
# ════════════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Colours ───────────────────────────────────────────────────────────────────
BOLD=$'\e[1m'; RESET=$'\e[0m'
GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RED=$'\e[31m'; CYAN=$'\e[36m'

# ── Usage ─────────────────────────────────────────────────────────────────────
usage() {
  cat <<EOF
Usage: $0 <backup.tar.gz>

Restore the FDE stack from a backup archive created by backup.sh.

Steps performed:
  1. Extract backup archive to temp dir
  2. Run deploy.sh  (fresh cluster installation)
  3. Wait for all pods to be Ready
  4. Restore ClickHouse data
  5. Restore Qdrant collection snapshots
  6. Restore Apache AGE (PostgreSQL) database
  7. Done

Arguments:
  <backup.tar.gz>   Path to the backup archive
EOF
  exit 0
}

[[ $# -lt 1 || "$1" == "--help" || "$1" == "-h" ]] && usage

BACKUP="$1"
[[ ! -f "$BACKUP" ]] && { echo "${RED}ERROR${RESET}: File not found: $BACKUP" >&2; exit 1; }

# ── Logging ───────────────────────────────────────────────────────────────────
phase() { echo -e "\n${BOLD}${CYAN}════ $* ${RESET}"; }
info()  { echo -e "  ${BOLD}→${RESET}  $*"; }
ok()    { echo -e "  ${GREEN}✔${RESET}  $*"; }
warn()  { echo -e "  ${YELLOW}!${RESET}  $*"; }
die()   { echo -e "  ${RED}✘${RESET}  $*" >&2; exit 1; }

# ── Cleanup ───────────────────────────────────────────────────────────────────
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
  pod=$(kubectl get pods -n "$ns" \
    -l "app.kubernetes.io/instance=${release}" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  [[ -n "$pod" ]] && { echo "$pod"; return 0; }
  pod=$(kubectl get pods -n "$ns" --no-headers \
    -o custom-columns=":metadata.name" 2>/dev/null \
    | grep "^${release}" | head -1 || true)
  [[ -n "$pod" ]] && { echo "$pod"; return 0; }
  echo ""
}

# ── Helper: wait until all uns pods are Running/Completed ────────────────────
wait_pods_ready() {
  local ns="${1:-uns}" timeout="${2:-300}"
  info "Waiting for pods in $ns to settle (max ${timeout}s)…"
  local deadline=$(( $(date +%s) + timeout ))
  while true; do
    local not_ready
    not_ready=$(kubectl get pods -n "$ns" --no-headers 2>/dev/null \
      | grep -cv -E "(Running|Completed|^$)" || true)
    [[ "$not_ready" -eq 0 ]] && { ok "All pods ready"; return 0; }
    [[ $(date +%s) -gt "$deadline" ]] && {
      warn "Timeout — $not_ready pod(s) still not ready; continuing anyway"
      return 0
    }
    sleep 5
  done
}

# ── 1. Extract ────────────────────────────────────────────────────────────────
phase "1: Extract backup"
TMPDIR="$(mktemp -d /tmp/fde-restore-XXXXXX)"
info "Archive  : $BACKUP"
info "Work dir : $TMPDIR"
tar -xzf "$BACKUP" -C "$TMPDIR"
ok "Extracted"
ls "$TMPDIR"

# ── 2. Deploy fresh stack ─────────────────────────────────────────────────────
phase "2: Deploy fresh stack"
DEPLOY="$SCRIPT_DIR/deploy.sh"
[[ -x "$DEPLOY" ]] || die "deploy.sh not found or not executable: $DEPLOY"
info "Running deploy.sh…"
bash "$DEPLOY"
ok "Deploy complete"

# ── 3. Wait for pods ──────────────────────────────────────────────────────────
phase "3: Wait for pods"
wait_pods_ready uns 300

# ── 4. Restore ClickHouse ─────────────────────────────────────────────────────
phase "4: ClickHouse restore"
CH_POD=$(find_pod fde-clickhouse)
if [[ -z "$CH_POD" ]]; then
  warn "fde-clickhouse pod not found — skipping"
elif [[ ! -d "$TMPDIR/clickhouse" ]]; then
  warn "No ClickHouse data in backup archive — skipping"
else
  info "Pod: $CH_POD"
  # Check for native backup dump (*.dump or any file from BACKUP ALL)
  NATIVE_FILES=$(find "$TMPDIR/clickhouse" -not -name "*.csv" -not -name "*.tsv" \
    -not -name "tables.tsv" -type f 2>/dev/null || true)
  if [[ -n "$NATIVE_FILES" ]]; then
    info "Restoring from native BACKUP ALL dump…"
    while IFS= read -r local_file; do
      remote_path="/var/lib/clickhouse/disks/backups/fde-backup/$(basename "$local_file")"
      kubectl cp "$local_file" "uns/$CH_POD:$(dirname "$remote_path")/$(basename "$local_file")" 2>/dev/null || true
    done <<< "$NATIVE_FILES"
    kubectl exec -n uns "$CH_POD" -- \
      clickhouse-client --query "RESTORE ALL FROM Disk('backups', 'fde-backup')" \
      && ok "ClickHouse restored from native backup" \
      || warn "RESTORE ALL failed — check ClickHouse logs"
  else
    # CSV fallback: re-insert CSV exports
    info "Restoring from CSV exports…"
    for csv in "$TMPDIR/clickhouse/"*.csv; do
      [[ -f "$csv" ]] || continue
      fname=$(basename "$csv" .csv)
      # fname format: db__table
      db="${fname%%__*}"; tbl="${fname##*__}"
      info "Inserting ${db}.${tbl}…"
      kubectl cp "$csv" "uns/$CH_POD:/tmp/${fname}.csv"
      kubectl exec -n uns "$CH_POD" -- \
        clickhouse-client \
          --query "INSERT INTO ${db}.${tbl} FORMAT CSVWithNames" \
          < <(kubectl exec -n uns "$CH_POD" -- cat "/tmp/${fname}.csv") \
        2>/dev/null \
        && ok "Inserted ${db}.${tbl}" \
        || warn "Failed to insert ${db}.${tbl}"
    done
  fi
fi

# ── 5. Restore Qdrant snapshots ───────────────────────────────────────────────
phase "5: Qdrant restore"
QDRANT_POD=$(find_pod fde-qdrant)
if [[ -z "$QDRANT_POD" ]]; then
  warn "fde-qdrant pod not found — skipping"
elif [[ ! -d "$TMPDIR/qdrant" ]] || ! compgen -G "$TMPDIR/qdrant/*" >/dev/null 2>&1; then
  warn "No Qdrant snapshots in backup archive — skipping"
else
  LOCAL_PORT=16333
  kubectl port-forward -n uns "$QDRANT_POD" "${LOCAL_PORT}:6333" &>/dev/null &
  BGPIDS+=($!)
  sleep 3
  for snap in "$TMPDIR/qdrant/"*; do
    [[ -f "$snap" ]] || continue
    fname=$(basename "$snap")
    coll="${fname%%---*}"
    info "Uploading snapshot for collection: $coll"
    # Upload snapshot (creates/recovers the collection)
    RESULT=$(curl -s -X POST \
      "http://localhost:${LOCAL_PORT}/collections/${coll}/snapshots/upload?priority=snapshot" \
      -H 'Content-Type: multipart/form-data' \
      -F "snapshot=@$snap" || true)
    echo "$RESULT" | python3 -c \
      "import sys,json; r=json.load(sys.stdin); print('  ok' if r.get('result') else f'  warn: {r}')" \
      2>/dev/null || true
    ok "Snapshot uploaded: $coll"
  done
  kill "${BGPIDS[-1]}" 2>/dev/null || true
  unset 'BGPIDS[-1]'
fi

# ── 6. Restore Apache AGE (PostgreSQL) ───────────────────────────────────────
phase "6: Apache AGE (PostgreSQL) restore"
AGE_POD=$(find_pod fde-age)
DUMP="$TMPDIR/age/fde-age.dump"
if [[ -z "$AGE_POD" ]]; then
  warn "fde-age pod not found — skipping"
elif [[ ! -f "$DUMP" ]]; then
  warn "No AGE dump in backup archive — skipping"
else
  info "Pod: $AGE_POD"
  kubectl cp "$DUMP" "uns/$AGE_POD:/tmp/fde-age.dump"
  kubectl exec -n uns "$AGE_POD" -- \
    pg_restore -U postgres --clean --if-exists \
    --dbname=postgres /tmp/fde-age.dump 2>/dev/null \
    && ok "PostgreSQL/AGE database restored" \
    || warn "pg_restore completed with warnings — verify manually"
fi

echo ""
echo "════ Restore complete. ═════════════════════════════════════════════════"
echo ""
echo "  Verify:  kubectl get pods -n uns"
echo "           helm list -A"
echo ""
