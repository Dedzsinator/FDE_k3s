# FDE Stack — Architecture Reference
> Generated: 2026-07-23 | Cluster: essen-fde (192.168.100.12) | Namespace: uns

## Physical topology

| Host | IP | Role | OS |
|---|---|---|---|
| nandor / essen-fde | 192.168.100.12 | K8s single-node (kubeadm) | Ubuntu 26.04 x86_64 |
| gyberrange-demo | 192.168.100.207 | Visualization (Godot 4.7.1 + OIP) | Ubuntu/Debian |
| spark2 | 10.200.0.11 | Bare-metal services: Timebase, MonsterMQ | Linux aarch64 |
| spark-acn | (dev box) | Claude Code / engineering workstation | Linux x86_64 |

## K8s namespace: uns

### Data ingestion pipeline

```
gyberrange-demo (dcsim)
  └─ SpB MQTT publish ──► EMQX 5.8 Community (NodePort 31883, stays open)
                               │
                         fde-emqx-svc:1883 (in-cluster)
                               │
                    spb-bridge container (in fde-mcp-servers pod)
                    • SpbCodec.decode_payload()
                    • INSERT INTO uns.sensor_raw (ClickHouse)
                               │
                    ClickHouse (StatefulSet, PVC)
                    • uns.sensor_raw table
                               │
                    predmaint (Deployment, replicas=1)
                    • ECOD+COPOD+IsolationForest ensemble (pyod+sklearn)
                    • Reads from Timebase (spark2:4511) + ClickHouse
                    • Publishes alarms to EMQX (in-cluster 1883)
                    • Topic: dc/blans/<area>/<line>/alarms/predmaint/<device>/<metric>
                               │
                    EMQX 31883 (NodePort, external)
                               │
                    gyberrange-demo (Godot OIP dc_blans_scene_v3.gd)
                    • subscribe_raw("dc/blans/+/+/alarms/#")
                    • _on_predmaint_alarm() → station color overlays
```

### Knowledge / AI pipeline

```
SpB data → ClickHouse
          ↓
Qdrant (StatefulSet, PVC)    ←── embed-api (nomic-embed-text-v1.5, port 8001)
Neo4j  (StatefulSet, PVC)        MCP tools: qdrant-mcp, neo4j-mcp
          ↓
fde-mcp-servers pod (FastMCP 3.3.1, streamable-http)
• obsidian-mcp
• rag-mcp
• emqx-mcp
• clickhouse-mcp
• fs-mcp
• predmaint (port 5100, also standalone Deployment)
```

### Observability pipeline

```
All pods → Promtail DaemonSet → Loki (in uns)
Prometheus Operator (kube-prometheus-stack)
• ServiceMonitors → predmaint:9090, emqx:18083, clickhouse:9363, qdrant:6333, neo4j:2004
• node-exporter, kube-state-metrics
Grafana (NodePort 32300 / grafana.fde-essen.local:443)
• 7 dashboards: cluster-health, emqx, clickhouse, qdrant-neo4j, spb-pipeline, predmaint, e2e-traceability
```

## Service inventory

| Service | Kind | Port(s) | Ingress host | Notes |
|---|---|---|---|---|
| fde-emqx | StatefulSet | 1883 (MQTT in-cluster), 8883 (TLS), 18083 (mgmt), **31883 NodePort** | emqx.fde-essen.local | NodePort MUST stay; dcsim/OIP point at it |
| fde-clickhouse | StatefulSet | 8123 (HTTP), 9000 (native), 9363 (prom) | clickhouse.fde-essen.local | PVC: clickhouse-data |
| fde-qdrant | StatefulSet | 6333 (HTTP+metrics), 6334 (gRPC) | qdrant.fde-essen.local | PVC: qdrant-storage |
| fde-neo4j | StatefulSet | 7474 (browser), 7687 (Bolt), 2004 (prom) | neo4j.fde-essen.local | PVC: neo4j-data |
| fde-mcp-servers | Deployment | 5100 (MCP), 8001 (embed-api) | mcp.fde-essen.local/* | contains spb-bridge + all MCP tools |
| predmaint | Deployment | 5100 (MCP), 9090 (metrics) | mcp.fde-essen.local/predmaint | replicas=1 (see Known Limitations) |
| kube-prometheus-stack | — | 9090 (Prom), 32300 NodePort (Grafana) | grafana.fde-essen.local | Grafana admin: prom-operator |
| loki | StatefulSet | 3100 | — | Loki datasource auto-wired into Grafana |
| ingress-nginx | DaemonSet | 80, 443 (hostPort) | — | DaemonSet+hostPort avoids NodePort overhead |
| cert-manager | Deployment | — | — | ClusterIssuers: fde-selfsigned, fde-ca |

## TLS

All `*.fde-essen.local` services serve TLS via cert-manager `fde-ca` ClusterIssuer.
CA cert (self-signed, 90-day, ECDSA P-256):

```bash
# Retrieve CA cert for client import:
kubectl get secret fde-ca-tls -n cert-manager -o jsonpath='{.data.tls\.crt}' | base64 -d > fde-ca.crt
# Import on a client machine (Ubuntu/Debian):
sudo cp fde-ca.crt /usr/local/share/ca-certificates/fde-ca.crt && sudo update-ca-certificates
# Import in browser: Settings → Certificates → Import fde-ca.crt as CA
```

Subject: `O=FDE, CN=fde-ca` | Valid: 2026-07-23 → 2026-10-21

## Portability (drag-drop / purge-reinstantiate)

Scripts live in `bin/`:

| Script | Purpose |
|---|---|
| `bin/deploy.sh` | Full stack install on fresh K8s node |
| `bin/purge.sh --yes-i-really-mean-it` | Wipe everything (add --keep-data to skip PVC delete) |
| `bin/backup.sh` | ClickHouse + Qdrant + Neo4j snapshots + Helm values → tarball |
| `bin/restore.sh <archive>` | Deploy fresh + restore data from tarball |
| `bin/recon.sh` | Inventory: pods, PVCs, services, Helm releases, resource usage |

## predmaint alarm topic scheme

Input (from dcsim via SpB): `spBv1.0/dc/blans/DDATA/<device>`

Output alarm topics: `dc/blans/<area>/<line>/alarms/predmaint/<device>/<metric>`

Example: `dc/blans/kill/clean/alarms/predmaint/stamp-1/ink_pct`

Alarm payload (JSON):
```json
{
  "severity": "WARNING",
  "health_index": 0.63,
  "score": 0.72,
  "sensor": "dc/blans/kill/clean/stamp-1/ink_pct",
  "ts": "2026-07-23T10:00:00Z",
  "model": "ensemble",
  "rul_hours": 52.0
}
```

## Known limitations / future work

- **predmaint is replicas=1**: In-process alarm de-duplication state. Scaling requires externalizing `_alarm_state` to Redis or using MQTT retained-message de-duplication.
- **CA cert expires 2026-10-21**: Renew by deleting the `fde-ca-cert` Certificate object (cert-manager auto-reissues).
- **Timebase + MonsterMQ are on spark2**: They are not in-cluster. Referenced via bare IP. Future: add ExternalName Services + DNS.
- **No distributed tracing**: Observability uses metric label correlation + Loki label correlation for "near-trace" forensics via the e2e-traceability dashboard. OTEL/Tempo integration is a future upgrade path.
- **No GitOps**: `bin/deploy.sh` is manual. Flux/ArgoCD is the natural v2.
- **Alertmanager**: Routes to null receiver by default. Slot in a Slack webhook at `alertmanager.fde-essen.local/config`.
