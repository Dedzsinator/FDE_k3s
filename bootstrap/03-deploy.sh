#!/usr/bin/env bash
# Bootstrap script 3/3 — Deploy the FDE stack via Helm.
# Run from the fde-k8s/ directory on the control-plane node (or any machine
# with kubectl access to the cluster).
#
# Usage:
#   ./bootstrap/03-deploy.sh [single|multi] [RELEASE_NAME] [NAMESPACE]
#
# Examples:
#   ./bootstrap/03-deploy.sh single          # single-node, release=fde, ns=uns
#   ./bootstrap/03-deploy.sh multi fde uns   # multi-node with Longhorn + TLS

set -euo pipefail

MODE="${1:-single}"
RELEASE="${2:-fde}"
NAMESPACE="${3:-uns}"

CHART_DIR="$(cd "$(dirname "$0")/.." && pwd)/charts/fde-stack"

echo "==> Resolving Helm chart dependencies..."
# Build local file:// dependencies for each sub-chart
for chart_dir in "$(dirname "$CHART_DIR")"/*; do
  if [[ -f "${chart_dir}/Chart.yaml" && "${chart_dir}" != "${CHART_DIR}" ]]; then
    echo "    packaging $(basename ${chart_dir})"
    helm package "${chart_dir}" --destination "${CHART_DIR}/charts/" --quiet
  fi
done

echo "==> Deploying FDE stack (mode=${MODE}, release=${RELEASE}, namespace=${NAMESPACE})"

BASE_FLAGS=(
  upgrade --install "${RELEASE}" "${CHART_DIR}"
  --namespace "${NAMESPACE}"
  --create-namespace
  --wait --timeout 10m
)

if [[ "${MODE}" == "multi" ]]; then
  helm "${BASE_FLAGS[@]}" \
    -f "${CHART_DIR}/values.yaml" \
    -f "${CHART_DIR}/values-multi-node.yaml"
else
  helm "${BASE_FLAGS[@]}" \
    -f "${CHART_DIR}/values.yaml" \
    -f "${CHART_DIR}/values-single-node.yaml"
fi

echo ""
echo "================================================================"
echo " FDE stack deployed!"
echo ""
echo " Services:"
echo "   Heimdall (dashboard):  http://fde.local  (or node IP)"
echo "   MonsterMQ GraphQL:     http://mqtt.fde.local"
echo "   Ignition:              http://ignition.fde.local"
echo "   Neo4j Browser:         http://neo4j.fde.local"
echo "   TimeBase Explorer:     http://timebase.fde.local"
echo "   pgAdmin:               http://pgadmin.fde.local"
echo "   MaestroHub (n8n):      http://maestro.fde.local"
echo "   Raw MQTT (NodePort):   <node-ip>:31883"
echo ""
echo " Add these to /etc/hosts or your DNS, pointing to the primary node IP:"
echo "   $(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')  fde.local mqtt.fde.local ignition.fde.local neo4j.fde.local timebase.fde.local pgadmin.fde.local maestro.fde.local litmus.fde.local predmaint.fde.local"
echo "================================================================"
