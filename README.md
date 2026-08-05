# fde-k8s — Factory Digital Edge · Kubernetes Stack

Accenture Factory Digital Edge (FDE) platform — single-node Kubernetes deployment
for industrial IoT, UNS (Unified Namespace), ML-based predictive maintenance, and
a gyberrange/OIP live visualization.

## Stack

| Component | Role | Exposure |
|---|---|---|
| **EMQX 5.8** | SparkplugB MQTT broker, UNS entry point | NodePort 31883 |
| **spb_bridge** | SparkplugB → ClickHouse decoder | internal |
| **ClickHouse** | Time-series historian (`uns.sensor_raw`) | NodePort 32123 |
| **predmaint** | ML predictive-maintenance MCP server (v1.3.0) | NodePort 32100 |
| **Qdrant** | Vector store (embeddings, RAG) | NodePort 32333 |
| **Apache AGE** | Graph DB (UNS topology) | NodePort 32432 |
| **Neo4j** | Graph DB (Obsidian vault knowledge graph) | ClusterIP |
| **Grafana** | Dashboards (7 provisioned) | NodePort 32300 |
| **Prometheus** | Metrics scrape | NodePort 32090 |
| **Loki** | Log aggregation | internal |
| **ingress-nginx** | TLS ingress | 443 |
| **cert-manager** | Self-signed CA | internal |

## Quick start

```bash
# Clone on the K8s node
git clone <this-repo> /home/nandor/fde-k8s
cd /home/nandor/fde-k8s

# Interactive TUI install (sets passwords, detects node IP)
sudo ./install.sh

# Or headless
sudo ./install.sh --ip 192.168.1.10 --skip-confirm
```

All passwords default to `CHANGEME` in values.yaml — `install.sh` generates
random 24-char secrets at deploy time and overrides them via `helm --set`.

## predmaint — ML Predictive Maintenance

FastMCP streamable-HTTP server (port 5100 / NodePort 32100).
All 10 MCP tools read exclusively from ClickHouse `uns.sensor_raw`.

**Algorithm pipeline:**
1. ClickHouse 5-min avg timeseries → `values[]` array
2. Ensemble anomaly score: `(ECOD + IsolationForest) / 2` → [0, 1] per bucket
3. Health Index: `100 − anomaly_rate×40 − |slope|×200 − volatility×15 − mean_score×25` → [0, 100]
4. RUL: OLS linear fit on raw values → time-to-3σ-threshold in hours
5. Alarm: severity transition → MQTT retain publish to EMQX

**Tools:** `list_sensors`, `find_sensors`, `get_sensor_now`, `analyze_sensor`,
`analyze_asset`, `detect_anomalies`, `maintenance_recommendations`,
`query_history`, `alarm_status`, `service_info`

## Portability

```bash
bin/deploy.sh       # full fresh deploy on a new K8s node
bin/purge.sh --yes-i-really-mean-it
bin/backup.sh       # CH + Qdrant + Neo4j snapshots → tarball
bin/restore.sh <archive>
bin/recon.sh        # cluster inventory vs expected state
```

## Bootstrap (fresh node)

```bash
bootstrap/01-init-cluster.sh   # kubeadm init, CNI, local registry
bootstrap/02-init-worker.sh    # join additional nodes
bootstrap/03-deploy.sh         # deploy FDE stack
```

## Constraints

- EMQX NodePort 31883 is **fixed** — dcsim and OIP configs point at it.
- `spb_bridge` is required — EMQX 5.8 Community cannot decode SparkplugB natively.
- predmaint runs 1 replica (in-process alarm dedup state).
