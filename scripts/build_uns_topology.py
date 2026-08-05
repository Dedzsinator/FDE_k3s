#!/usr/bin/env python3
"""
build_uns_topology.py — Discovers UNS topology from ClickHouse sensor_raw
and writes it as a graph into Neo4j.

Run from inside the fde-mcp-servers pod (neo4j-mcp container), or any host
where the neo4j Python driver is installed.

Usage:
  kubectl exec -n uns <pod> -c neo4j-mcp -- python3 /app/build_uns_topology.py

Environment variables (optional, defaults shown):
  CH_URL        http://127.0.0.1:32123
  CH_USER       default
  CH_PASSWORD   fde-clickhouse-secret
  NEO4J_URI     bolt://127.0.0.1:32687
  NEO4J_USER    neo4j
  NEO4J_PW      fde-neo4j-secret
"""
import json, os, urllib.request, urllib.parse, sys
from neo4j import GraphDatabase

CH       = os.getenv("CH_URL",      "http://127.0.0.1:32123")
CH_USER  = os.getenv("CH_USER",     "default")
CH_PW    = os.getenv("CH_PASSWORD", "fde-clickhouse-secret")
NEO4J    = os.getenv("NEO4J_URI",   "bolt://127.0.0.1:32687")
N4J_USER = os.getenv("NEO4J_USER",  "neo4j")
N4J_PW   = os.getenv("NEO4J_PW",    "fde-neo4j-secret")

def ch(sql):
    url = f"{CH}/?user={CH_USER}&password={CH_PW}&query=" + urllib.parse.quote(sql)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.read().decode()
    except Exception as e:
        print(f"  CH error: {e}", file=sys.stderr)
        return ""

def ch_rows(sql):
    return [json.loads(l) for l in ch(sql).splitlines() if l.startswith("{")]

# ── Discover SparkplugB topology ──────────────────────────────────────────────
print("Querying ClickHouse for SpB topology...")
topo_rows = ch_rows(
    "SELECT "
    "splitByChar('/', topic)[2] AS grp, "
    "splitByChar('/', topic)[3] AS msg_type, "
    "splitByChar('/', topic)[4] AS edge_node, "
    "if(length(splitByChar('/', topic))>=5, splitByChar('/', topic)[5], '') AS device, "
    "count() AS cnt "
    "FROM uns.sensor_raw "
    "WHERE topic LIKE 'spBv1.0/%' AND length(splitByChar('/', topic))>=4 "
    "GROUP BY grp, msg_type, edge_node, device ORDER BY grp, edge_node, device "
    "FORMAT JSONEachRow"
)

metrics_map = {
    f"{r['grp']}/{r['edge_node']}/{r['device']}": r
    for r in ch_rows(
        "SELECT "
        "splitByChar('/', topic)[2] AS grp, "
        "splitByChar('/', topic)[4] AS edge_node, "
        "splitByChar('/', topic)[5] AS device, "
        "groupUniqArray(tag) AS metrics, count() AS rows "
        "FROM uns.sensor_raw "
        "WHERE topic LIKE 'spBv1.0/%/DDATA/%' AND length(splitByChar('/', topic))>=5 AND tag!='' "
        "GROUP BY grp, edge_node, device FORMAT JSONEachRow"
    )
}

# ── Discover UNS-format topics (/.Org.Site.Area.*) ───────────────────────────
print("Querying ClickHouse for UNS-format topics...")
uns_rows = ch_rows(
    "SELECT "
    "replaceRegexpOne(topic, '^/\\.', '') AS path, "
    "splitByChar('.', replaceRegexpOne(topic, '^/\\.', ''))[1] AS org, "
    "splitByChar('.', replaceRegexpOne(topic, '^/\\.', ''))[2] AS site, "
    "splitByChar('.', replaceRegexpOne(topic, '^/\\.', ''))[3] AS area, "
    "splitByChar('.', replaceRegexpOne(topic, '^/\\.', ''))[4] AS device, "
    "groupUniqArray(tag) AS metrics, count() AS rows "
    "FROM uns.sensor_raw WHERE topic LIKE '/.%.%.%' "
    "GROUP BY path, org, site, area, device "
    "HAVING org!='' AND site!='' AND area!='' AND device!='' "
    "FORMAT JSONEachRow"
)

# ── Build topology summary ────────────────────────────────────────────────────
topology = {}
for r in topo_rows:
    g, en, dev = r["grp"], r["edge_node"], r["device"]
    topology.setdefault(g, {}).setdefault(en, set())
    if dev:
        topology[g][en].add(dev)

print(f"\nSpB groups: {sorted(topology)}")
for g in sorted(topology):
    for en in sorted(topology[g]):
        print(f"  {g}/{en}: {len(topology[g][en])} devices")

print(f"UNS-format rows: {len(uns_rows)}")

# ── Write to Neo4j ────────────────────────────────────────────────────────────
print("\nWriting topology to Neo4j...")
driver = GraphDatabase.driver(NEO4J, auth=(N4J_USER, N4J_PW))

with driver.session() as s:
    for stmt in [
        "CREATE CONSTRAINT uns_group_name IF NOT EXISTS FOR (n:Group) REQUIRE n.name IS UNIQUE",
        "CREATE CONSTRAINT uns_edge_node_uid IF NOT EXISTS FOR (n:EdgeNode) REQUIRE n.uid IS UNIQUE",
        "CREATE CONSTRAINT uns_device_uid IF NOT EXISTS FOR (n:Device) REQUIRE n.uid IS UNIQUE",
        "CREATE CONSTRAINT uns_metric_uid IF NOT EXISTS FOR (n:Metric) REQUIRE n.uid IS UNIQUE",
    ]:
        try:
            s.run(stmt)
        except Exception:
            pass  # already exists

    # SpB nodes
    for g in topology:
        s.run("MERGE (n:Group {name:$n}) SET n.protocol='SparkplugB'", n=g)
        for en in topology[g]:
            en_uid = f"spb/{g}/{en}"
            s.run(
                "MERGE (n:EdgeNode {uid:$uid}) SET n.name=$name, n.group=$g, n.protocol='SparkplugB' "
                "WITH n MATCH (g:Group {name:$g}) MERGE (g)-[:HAS_NODE]->(n)",
                uid=en_uid, name=en, g=g
            )
            for dev in topology[g][en]:
                dev_uid = f"spb/{g}/{en}/{dev}"
                s.run(
                    "MERGE (d:Device {uid:$uid}) SET d.name=$name, d.edge_node=$en, d.group=$g, d.protocol='SparkplugB' "
                    "WITH d MATCH (n:EdgeNode {uid:$nuid}) MERGE (n)-[:HAS_DEVICE]->(d)",
                    uid=dev_uid, name=dev, en=en, g=g, nuid=en_uid
                )
                for m in metrics_map.get(f"{g}/{en}/{dev}", {}).get("metrics", []):
                    if not m:
                        continue
                    s.run(
                        "MERGE (m:Metric {uid:$uid}) SET m.name=$name, m.device_uid=$duid "
                        "WITH m MATCH (d:Device {uid:$duid}) MERGE (d)-[:HAS_METRIC]->(m)",
                        uid=f"spb/{g}/{en}/{dev}/{m}", name=m, duid=dev_uid
                    )

    # UNS-format nodes
    for r in uns_rows:
        org, site, area, dev = r["org"], r["site"], r["area"], r["device"]
        grp = f"{org}.{site}"
        s.run("MERGE (n:Group {name:$n}) SET n.protocol='UNS'", n=grp)
        area_uid = f"uns/{org}/{site}/{area}"
        s.run(
            "MERGE (n:EdgeNode {uid:$uid}) SET n.name=$name, n.group=$g, n.protocol='UNS' "
            "WITH n MATCH (g:Group {name:$g}) MERGE (g)-[:HAS_NODE]->(n)",
            uid=area_uid, name=area, g=grp
        )
        dev_uid = f"uns/{org}/{site}/{area}/{dev}"
        s.run(
            "MERGE (d:Device {uid:$uid}) SET d.name=$name, d.edge_node=$area, d.group=$g, d.protocol='UNS' "
            "WITH d MATCH (n:EdgeNode {uid:$nuid}) MERGE (n)-[:HAS_DEVICE]->(d)",
            uid=dev_uid, name=dev, area=area, g=grp, nuid=area_uid
        )
        for m in r.get("metrics", []):
            if not m:
                continue
            s.run(
                "MERGE (m:Metric {uid:$uid}) SET m.name=$name, m.device_uid=$duid "
                "WITH m MATCH (d:Device {uid:$duid}) MERGE (d)-[:HAS_METRIC]->(m)",
                uid=f"uns/{org}/{site}/{area}/{dev}/{m}", name=m, duid=dev_uid
            )

    # Summary
    counts = {
        lbl: s.run(f"MATCH (n:{lbl}) RETURN count(n) AS c").single()["c"]
        for lbl in ["Group", "EdgeNode", "Device", "Metric"]
    }

driver.close()
print(f"\nNeo4j: {counts}")
print("Done.")
