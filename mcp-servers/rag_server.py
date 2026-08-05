"""
FDE RAG MCP Server
Indexes OT sensor history and Ignition metadata into Qdrant,
then serves semantic retrieval as MCP tools for Claude.
"""
import hashlib
import json
import logging
import os
import sys
import urllib.parse
import urllib.request
from typing import Optional

os.environ["FASTMCP_DISABLE_BANNER"] = "1"
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

from fastembed import TextEmbedding
from fastmcp import FastMCP
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

# ── Config ────────────────────────────────────────────────────────────────────
QDRANT_URL      = os.environ.get("QDRANT_URL",      "http://127.0.0.1:32333")
CH_URL          = os.environ.get("CH_URL",           "http://127.0.0.1:32123")
CH_USER         = os.environ.get("CH_USER",          "default")
CH_PASS         = os.environ.get("CH_PASSWORD",      "fde-clickhouse-secret")
IGN_URL         = os.environ.get("IGN_URL",          "http://127.0.0.1:30088/data/mcp/fde")
IGN_TOKEN       = os.environ.get("IGN_TOKEN",        "MCP:Nc8_QIEZDNJcLFLbLzHCepeZWpuRNlZTCfd1XaYLWwE")
VEC_DIM         = 384
COL_SENSORS     = "fde_sensor_events"
COL_IGNITION    = "fde_ignition_metadata"

mcp = FastMCP("fde-rag")

_embedder: Optional[TextEmbedding] = None
_qdrant:   Optional[QdrantClient]  = None


def _embed() -> TextEmbedding:
    global _embedder
    if _embedder is None:
        _embedder = TextEmbedding()
    return _embedder


def _qd() -> QdrantClient:
    global _qdrant
    if _qdrant is None:
        _qdrant = QdrantClient(url=QDRANT_URL)
    return _qdrant


def _ensure_col(name: str):
    existing = {c.name for c in _qd().get_collections().collections}
    if name not in existing:
        _qd().create_collection(
            name, vectors_config=VectorParams(size=VEC_DIM, distance=Distance.COSINE)
        )


def _stable_id(text: str) -> int:
    return int(hashlib.md5(text.encode()).hexdigest()[:16], 16) >> 1


def _ch(sql: str) -> str:
    url = (
        f"{CH_URL}/?user={urllib.parse.quote(CH_USER)}"
        f"&password={urllib.parse.quote(CH_PASS)}"
    )
    req = urllib.request.Request(
        url, data=sql.strip().encode(),
        headers={"Content-Type": "application/octet-stream"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode().strip()
    except urllib.request.HTTPError as e:
        raise RuntimeError(f"ClickHouse error: {e.read().decode()[:300]}")


# ── Ignition MCP session ──────────────────────────────────────────────────────
def _ign_session():
    """Open a new Ignition MCP session, return sid."""
    def _post(payload, sid=None):
        req = urllib.request.Request(
            IGN_URL,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "X-Ignition-API-Token": IGN_TOKEN,
                **({"Mcp-Session-Id": sid} if sid else {}),
            },
        )
        resp = urllib.request.urlopen(req, timeout=30)
        hdrs = dict(resp.headers)
        try:
            body = resp.read().decode().replace("event: message\ndata: ", "").strip()
        except Exception:
            body = ""
        return hdrs, json.loads(body) if body else {}

    hdrs, _ = _post({
        "jsonrpc": "2.0", "id": 0, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "fde-rag", "version": "1"}},
    })
    sid = hdrs.get("Mcp-Session-Id")
    _post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, sid)
    return sid, _post


def _ign_call(tool: str, args: dict, sid, post_fn) -> dict:
    _, data = post_fn({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }, sid)
    if "result" not in data:
        return {}
    content = data["result"].get("content", [{}])
    text = content[0].get("text", "") if content else ""
    try:
        return json.loads(text)
    except Exception:
        return {"_raw": text}


# ── Tools ─────────────────────────────────────────────────────────────────────
@mcp.tool()
def sync_clickhouse_events(hours_back: int = 168) -> str:
    """
    Sync recent sensor events and alarms from ClickHouse into Qdrant.
    Indexes each unique (topic, tag, value) event as a searchable document.
    Default: last 7 days (168 hours). Returns count of vectors upserted.
    """
    _ensure_col(COL_SENSORS)

    sql = f"""
        SELECT
            toString(ts) AS ts_str,
            topic,
            cell,
            tag,
            round(value_num, 3) AS val
        FROM uns.sensor_raw
        WHERE ts > now() - toIntervalHour({hours_back})
          AND value_num IS NOT NULL
        ORDER BY ts DESC
        LIMIT 3000
        FORMAT JSONEachRow
    """
    rows_raw = _ch(sql)
    if not rows_raw:
        return f"No sensor data found in the last {hours_back} hours."

    rows = [json.loads(l) for l in rows_raw.splitlines() if l.strip()]

    # Group by event: deduplicate on (topic, tag) keeping the latest + extremes
    groups: dict = {}
    for r in rows:
        key = f"{r['topic']}::{r['tag']}"
        if key not in groups:
            groups[key] = {"latest": r, "count": 0, "min": r["val"], "max": r["val"]}
        g = groups[key]
        g["count"] += 1
        g["min"] = min(g["min"], r["val"])
        g["max"] = max(g["max"], r["val"])

    texts = []
    payloads = []
    for key, g in groups.items():
        r = g["latest"]
        machine = r["topic"].split(".")[-2] if "." in r["topic"] else r["cell"]
        msg_type = r["topic"].split(".")[-1]
        text = (
            f"[{r['cell']} / {machine}] {r['tag']} = {r['val']} "
            f"({msg_type}) at {r['ts_str']}. "
            f"Last {hours_back}h: min={g['min']}, max={g['max']}, n={g['count']} readings."
        )
        texts.append(text)
        payloads.append({
            "ts": r["ts_str"], "topic": r["topic"], "cell": r["cell"],
            "machine": machine, "msg_type": msg_type,
            "tag": r["tag"], "value": r["val"],
            "min": g["min"], "max": g["max"], "count": g["count"],
            "text": text, "source": "clickhouse_sensor_raw",
        })

    vectors = list(_embed().embed(texts))
    points = [
        PointStruct(
            id=_stable_id(texts[i]),
            vector=vectors[i].tolist(),
            payload=payloads[i],
        )
        for i in range(len(texts))
    ]

    batch_size = 200
    for i in range(0, len(points), batch_size):
        _qd().upsert(COL_SENSORS, points[i : i + batch_size])

    return (
        f"Synced {len(points)} sensor event vectors into '{COL_SENSORS}' "
        f"(from {len(rows)} rows, {hours_back}h window)."
    )


@mcp.tool()
def sync_ignition_metadata() -> str:
    """
    Index Ignition MCP tool definitions, tag list, and available named queries
    into Qdrant. Enables semantic search over what Ignition can do and what
    tags/queries exist in the Essen cluster.
    """
    _ensure_col(COL_IGNITION)
    sid, post_fn = _ign_session()

    texts = []
    payloads = []

    # 1. MCP tool definitions
    _, tools_data = post_fn({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, sid)
    tools = tools_data.get("result", {}).get("tools", [])
    for t in tools:
        name = t.get("name", "")
        desc = t.get("description", "")
        # Summarise inputSchema
        schema = t.get("inputSchema", {})
        props = list(schema.get("properties", {}).keys())
        text = f"Ignition MCP tool: {name}. {desc[:300]} Parameters: {', '.join(props) or 'none'}."
        texts.append(text)
        payloads.append({"type": "mcp_tool", "name": name, "description": desc[:500],
                         "parameters": props, "text": text, "source": "ignition_mcp_tools"})

    # 2. Tag list
    tags_result = _ign_call("get_all_tags", {"provider": "default"}, sid, post_fn)
    tag_list = tags_result if isinstance(tags_result, list) else []
    for tag_path in tag_list:
        text = f"Ignition tag: {tag_path}"
        texts.append(text)
        payloads.append({"type": "tag", "path": tag_path,
                         "text": text, "source": "ignition_tags"})

    # 3. Named queries
    nq_result = _ign_call("list_named_queries", {}, sid, post_fn)
    nq_list = nq_result if isinstance(nq_result, list) else []
    for nq in nq_list:
        name = nq if isinstance(nq, str) else nq.get("name", str(nq))
        text = f"Ignition named query: {name}"
        texts.append(text)
        payloads.append({"type": "named_query", "name": name,
                         "text": text, "source": "ignition_named_queries"})

    # 4. Script modules
    sm_result = _ign_call("list_script_modules", {}, sid, post_fn)
    sm_list = sm_result if isinstance(sm_result, list) else sm_result.get("modules", [])
    for sm in sm_list[:20]:
        name = sm if isinstance(sm, str) else sm.get("name", str(sm))
        text = f"Ignition script module: {name}"
        texts.append(text)
        payloads.append({"type": "script_module", "name": name,
                         "text": text, "source": "ignition_script_modules"})

    if not texts:
        return "No Ignition metadata found to index."

    vectors = list(_embed().embed(texts))
    points = [
        PointStruct(
            id=_stable_id(texts[i]),
            vector=vectors[i].tolist(),
            payload=payloads[i],
        )
        for i in range(len(texts))
    ]

    batch_size = 100
    for i in range(0, len(points), batch_size):
        _qd().upsert(COL_IGNITION, points[i : i + batch_size])

    breakdown = {
        "mcp_tools": len(tools),
        "tags": len(tag_list),
        "named_queries": len(nq_list),
        "script_modules": len(sm_list),
    }
    return (
        f"Synced {len(points)} vectors into '{COL_IGNITION}'. "
        f"Breakdown: {json.dumps(breakdown)}"
    )


@mcp.tool()
def query_rag(question: str, top_k: int = 5) -> str:
    """
    Semantic search across all indexed FDE data (sensor history + Ignition metadata).
    Returns the most relevant context chunks for answering the question.
    Use this before answering questions about the OT cluster state, alarms,
    available Ignition tools, or tag configuration.
    """
    vec = next(_embed().embed([question])).tolist()

    fde_cols = [c.name for c in _qd().get_collections().collections
                if c.name.startswith("fde_")]
    if not fde_cols:
        return (
            "No data indexed yet. Run sync_clickhouse_events() and "
            "sync_ignition_metadata() first."
        )

    # Search each collection, merge by score
    all_hits = []
    for col in fde_cols:
        try:
            import urllib.request
            body = json.dumps({"vector": vec, "limit": top_k,
                               "with_payload": True}).encode()
            req = urllib.request.Request(
                f"{QDRANT_URL}/collections/{col}/points/search",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                hits = json.loads(r.read())["result"]
            for h in hits:
                all_hits.append((h["score"], col, h["payload"]))
        except Exception:
            pass

    if not all_hits:
        return "No relevant results found. Try syncing data first."

    all_hits.sort(reverse=True)
    lines = [f"Top {min(top_k, len(all_hits))} results for: \"{question}\"\n"]
    for score, col, payload in all_hits[:top_k]:
        text = payload.get("text", "") or payload.get("description", "")
        src  = payload.get("source", col)
        lines.append(f"[score={score:.3f}] [{src}]\n  {text}\n")

    return "\n".join(lines)


@mcp.tool()
def get_rag_stats() -> str:
    """
    Show which data is currently indexed in Qdrant for RAG,
    including collection names and vector counts.
    """
    cols = _qd().get_collections().collections
    if not cols:
        return "No collections in Qdrant. Run sync tools to populate."

    lines = ["Qdrant RAG collections:"]
    for c in cols:
        info = _qd().get_collection(c.name)
        count = getattr(info, "points_count", "?")
        lines.append(f"  {c.name}: {count} vectors")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=9107)
