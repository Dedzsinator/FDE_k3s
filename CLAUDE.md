# FDE Kubernetes Stack — Claude Code Operating Guide

This file tells Claude Code how to operate, inspect, and self-document the FDE k8s stack.
Read this first when working in this directory.

## What this repo is

Helm umbrella chart (`charts/fde-stack/`) that deploys the full FDE Industrial IoT platform
to a k3s Kubernetes cluster on the Spark nodes. It replaces the Docker Compose stack on spark2.

```
spark1 (192.168.100.10)  — vLLM inference (external to cluster)
spark2 (192.168.100.11)  — k3s control-plane + all OT services
```

## Helm chart map

| Chart | Replaces | Key port |
|---|---|---|
| `nats` | MonsterMQ | MQTT 31883 (NodePort), NATS 4222 |
| `clickhouse` | Timebase historian | HTTP 8123, native 9000 |
| `apache-age` | Neo4j | PostgreSQL 5432 |
| `qdrant` | — (new) | REST 6333, gRPC 6334 |
| `monitoring` | — (new) | Grafana 80, Prometheus 9090 |
| `loki` | — (new) | Loki 3100 |
| `dcsim` | dc-sim Docker | — |
| `predmaint` | predmaint Docker | 8080 |
| `ignition` | ignition Docker | 8088 |
| `litmusedge` | litmusedge Docker | 8080 |

## Bootstrap (run once)

```bash
sudo ./bootstrap/01-init-cluster.sh          # k3s + NGINX + cert-manager
./bootstrap/03-deploy.sh single              # deploy full stack
```

## Common operations

```bash
# Check all pods
kubectl get pods -n uns -o wide

# Watch rollout
kubectl rollout status deployment/fde-dcsim -n uns

# Tail predmaint logs
kubectl logs -n uns -l app.kubernetes.io/name=predmaint -f

# Hot-reload dc-sim degradation state (no restart needed)
kubectl edit configmap fde-dcsim-factory-state -n uns

# Force restart a deployment
kubectl rollout restart deployment/fde-predmaint -n uns

# Check HPA status
kubectl get hpa -n uns

# Check ClickHouse data freshness
kubectl exec -n uns fde-clickhouse-0 -- \
  clickhouse-client --query \
  "SELECT cell, max(ts), count() FROM uns.sensor_raw
   WHERE ts > now() - INTERVAL 1 HOUR GROUP BY cell"

# NATS JetStream stats
kubectl exec -n uns fde-nats-0 -- \
  curl -s http://localhost:8222/jsz | python3 -m json.tool

# Qdrant collections
kubectl exec -n uns fde-qdrant-0 -- \
  curl -s http://localhost:6333/collections

# Apache Age graph stats
kubectl exec -n uns fde-apache-age-0 -- \
  psql -U age factory_graph -c \
  "LOAD 'age'; SET search_path=ag_catalog,public;
   SELECT label, count(*) FROM ag_vertex WHERE graph_name='factory' GROUP BY label;"
```

## Autodoc — self-documenting the stack

When asked to "autodoc", "update the wiki", or "document the cluster state":

1. Run the kubectl and service health commands above
2. Open the Obsidian MCP server (`fde-obsidian`) and read `AGENTS.md`
3. Follow the **Autodoc** operation defined there
4. Overwrite `LLM-Wiki/wiki/k8s/cluster-state.md` with current state
5. Update any entity pages where state has changed
6. Append to `LLM-Wiki/log.md`

The full autodoc spec is in the Obsidian vault at `LLM-Wiki/wiki/k8s/autodoc.md`.

## Changing factory degradation state

The dc-sim degradation is controlled by the `fde-dcsim-factory-state` ConfigMap.
The sim hot-reloads it within 60 ticks (~60 seconds). No pod restart needed.

```bash
kubectl edit configmap fde-dcsim-factory-state -n uns
```

Key YAML fields inside `factory_state.yaml`:
- `mode: healthy` — lock station in healthy state forever
- `mode: failed` — immediately apply full failure offsets
- `degradation.trajectory: linear|logarithmic|exponential|weibull` — degradation curve
- `degradation.total_days: 180` — time from start_date to full failure

## Upgrading the stack

```bash
# After editing values.yaml or chart templates
helm upgrade fde ./charts/fde-stack -n uns

# Upgrade single chart only
helm upgrade fde ./charts/fde-stack -n uns \
  --set clickhouse.resources.limits.memory=16Gi
```

## Scaling

```bash
# Manual scale (overrides HPA temporarily)
kubectl scale deployment fde-dcsim -n uns --replicas=3

# Check HPA will kick back in
kubectl get hpa fde-dcsim -n uns
```

## Rollback

```bash
helm rollback fde -n uns        # roll back to previous release
helm history fde -n uns         # see release history
```

## Ingress hostnames to add to /etc/hosts

```
192.168.100.11  fde.local
192.168.100.11  grafana.fde.local
192.168.100.11  clickhouse.fde.local
192.168.100.11  qdrant.fde.local
192.168.100.11  alerts.fde.local
192.168.100.11  mqtt.fde.local
192.168.100.11  ignition.fde.local
192.168.100.11  neo4j.fde.local
192.168.100.11  timebase.fde.local
192.168.100.11  pgadmin.fde.local
192.168.100.11  maestro.fde.local
```

## Secrets

All default passwords are in each chart's `values.yaml`. Change before production:
```bash
helm upgrade fde ./charts/fde-stack -n uns \
  --set clickhouse.auth.password=my-real-password \
  --set apache-age.auth.password=my-real-password \
  --set qdrant.auth.apiKey=my-api-key
```

## Related files

- `charts/fde-stack/values.yaml` — master values (single-node defaults)
- `charts/fde-stack/values-single-node.yaml` — single-node overrides
- `charts/fde-stack/values-multi-node.yaml` — multi-node (Longhorn) overrides
- `architecture.html` — interactive architecture pitch doc (open in browser)
- Obsidian vault: `/opt/fde/obsidian/fde-vault/LLM-Wiki/wiki/k8s/`
