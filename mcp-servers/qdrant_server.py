#!/usr/bin/env python3
"""
FDE Qdrant MCP server — nandor k8s.

Semantic search over:
  - obsidian_notes   — vault wiki pages (LLM-Wiki + fde-stack + use-cases etc.)
  - sensor_topics    — MQTT topic paths from NATS (dc/blans/kill/...)
  - anomaly_sigs     — anomaly feature vectors (numeric, no embedding)

Transport: streamable-HTTP on localhost:9103
Requires: pip install fastmcp qdrant-client fastembed

Start:
    python3 qdrant_server.py
    # or via systemd: systemctl --user start fde-qdrant-mcp
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

os.environ["FASTMCP_DISABLE_BANNER"] = "1"
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

from fastmcp import FastMCP

mcp = FastMCP("fde-qdrant")

# ── Config ─────────────────────────────────────────────────────────────────────

QDRANT_URL   = os.getenv("QDRANT_URL",   "http://127.0.0.1:32333")
EMBED_MODEL  = os.getenv("EMBED_MODEL",  "BAAI/bge-small-en-v1.5")
VAULT_PATH   = os.getenv("VAULT_PATH",   "/opt/fde/obsidian/fde-vault")
OBS_API_BASE = os.getenv("OBS_API_BASE", "http://192.168.100.100:27123")
OBS_API_KEY  = os.getenv("OBS_API_KEY",  "a9148b82dd5e61801a167c230613c3d468da1ba27a922180f31ff5416c98c5ef")

COLL_NOTES   = "obsidian_notes"
COLL_SENSORS = "sensor_topics"
COLL_ANOMALY = "anomaly_signatures"


# ── Lazy singletons ────────────────────────────────────────────────────────────

_client   = None
_embedder = None


def _qdrant():
    global _client
    if _client is None:
        try:
            from qdrant_client import QdrantClient
        except ImportError:
            raise RuntimeError("pip install qdrant-client")
        _client = QdrantClient(url=QDRANT_URL, timeout=30)
    return _client


def _embed(texts: list[str]) -> list[list[float]]:
    global _embedder
    if _embedder is None:
        try:
            from fastembed import TextEmbedding
        except ImportError:
            raise RuntimeError("pip install fastembed")
        _embedder = TextEmbedding(model_name=EMBED_MODEL)
    return [v.tolist() for v in _embedder.embed(texts)]


def _vec(text: str) -> list[float]:
    return _embed([text])[0]


def _fmt_results(hits: list[Any], max_results: int) -> str:
    out = []
    for h in hits[:max_results]:
        p   = h.payload
        score = round(h.score, 3)
        out.append({
            "score":   score,
            "path":    p.get("path", p.get("topic", "?")),
            "heading": p.get("heading", ""),
            "text":    p.get("text", "")[:500],
            "tags":    p.get("tags", []),
            "links":   p.get("links", [])[:5],
        })
    return json.dumps(out, indent=2)


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool
def search_wiki(
    query: str,
    top_k: int = 5,
    tag_filter: str = "",
) -> str:
    """
    Semantic search over fde-vault wiki notes (obsidian_notes collection).

    Args:
        query:      Natural language query, e.g. "how does anomaly detection work"
        top_k:      Number of results (default 5, max 20)
        tag_filter: Restrict to notes with this tag (e.g. "predmaint", "use-case")

    Returns:
        JSON list of matching chunks with score, path, heading, text excerpt, tags.
    """
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    top_k = min(int(top_k), 20)
    vec   = _vec(query)

    q_filter = None
    if tag_filter:
        q_filter = Filter(must=[
            FieldCondition(key="tags", match=MatchValue(value=tag_filter))
        ])

    hits = _qdrant().search(
        collection_name=COLL_NOTES,
        query_vector=vec,
        limit=top_k,
        query_filter=q_filter,
        with_payload=True,
    )
    return _fmt_results(hits, top_k)


@mcp.tool
def search_sensors(
    query: str,
    top_k: int = 10,
) -> str:
    """
    Semantic search over MQTT sensor topic paths (sensor_topics collection).
    Finds relevant topics by natural-language description.

    Args:
        query: Natural language description, e.g. "scald tank temperature"
        top_k: Number of results (default 10)

    Returns:
        JSON list with score and MQTT topic path.
    """
    top_k = min(int(top_k), 30)
    vec   = _vec(query)
    hits = _qdrant().search(
        collection_name=COLL_SENSORS,
        query_vector=vec,
        limit=top_k,
        with_payload=True,
    )
    out = []
    for h in hits[:top_k]:
        out.append({
            "score":  round(h.score, 3),
            "topic":  h.payload.get("topic", ""),
            "text":   h.payload.get("text", "")[:200],
        })
    return json.dumps(out, indent=2)


@mcp.tool
def search_context(
    query: str,
    collections: str = "obsidian_notes,sensor_topics",
    top_k: int = 5,
) -> str:
    """
    Multi-collection semantic search. Returns combined ranked results.
    Useful for assembling RAG context that spans wiki knowledge AND sensor metadata.

    Args:
        query:       Natural language query
        collections: Comma-separated collection names (obsidian_notes, sensor_topics)
        top_k:       Results per collection (default 5)
    """
    colls = [c.strip() for c in collections.split(",") if c.strip()]
    vec   = _vec(query)
    top_k = min(int(top_k), 15)
    all_results = {}

    for coll in colls:
        try:
            hits = _qdrant().search(
                collection_name=coll,
                query_vector=vec,
                limit=top_k,
                with_payload=True,
            )
            all_results[coll] = json.loads(_fmt_results(hits, top_k))
        except Exception as e:
            all_results[coll] = {"error": str(e)}

    return json.dumps(all_results, indent=2)


@mcp.tool
def find_similar_anomalies(
    anomaly_rate: float,
    slope: float,
    health_index: float,
    rul_hours: float | None = None,
    top_k: int = 5,
) -> str:
    """
    Find previously recorded anomalies similar to the given feature vector.
    Uses the anomaly_signatures collection (numeric vectors, no text embedding).

    Args:
        anomaly_rate:  Recent anomaly fraction (0.0-1.0) from predmaint
        slope:         Normalised degradation slope from predmaint
        health_index:  0-100 health index value
        rul_hours:     Remaining useful life estimate (None -> use 8760 as "unknown")
        top_k:         Number of similar past anomalies to return
    """
    rul = float(rul_hours) if rul_hours is not None else 8760.0
    # Normalize to [0,1] for each dimension
    vec = [
        float(anomaly_rate),
        min(abs(float(slope)) * 10, 1.0),
        1.0 - float(health_index) / 100.0,
        1.0 - min(rul / 8760.0, 1.0),
    ]
    try:
        hits = _qdrant().search(
            collection_name=COLL_ANOMALY,
            query_vector=vec,
            limit=min(int(top_k), 10),
            with_payload=True,
        )
    except Exception as e:
        return json.dumps({"error": str(e), "hint": "anomaly_signatures collection may be empty — run predmaint and record signatures first"})

    out = []
    for h in hits:
        out.append({
            "score":      round(h.score, 3),
            "sensor":     h.payload.get("sensor", ""),
            "recorded_at": h.payload.get("recorded_at", ""),
            "outcome":    h.payload.get("outcome", ""),
            "notes":      h.payload.get("notes", ""),
        })
    return json.dumps(out, indent=2)


@mcp.tool
def ingest_note(
    vault_path: str,
    vault: str = "cyberrange",
) -> str:
    """
    Re-embed a single vault note and upsert to Qdrant obsidian_notes.
    vault_path is vault-relative, e.g. 'fde-stack/monstermq.md'.

    vault: 'cyberrange' (default, 192.168.100.100) for notes written via the
           obsidian MCP tool; 'fde' (192.168.100.100) for the main FDE knowledge
           base (architecture docs, use-cases, wiki pages).
    """
    import urllib.request
    import urllib.parse
    import hashlib
    import re

    # Select vault endpoint
    _VAULTS = {
        "fde":        ("http://192.168.100.100:27123", "a9148b82dd5e61801a167c230613c3d468da1ba27a922180f31ff5416c98c5ef"),
        "cyberrange": (OBS_API_BASE, OBS_API_KEY),
    }
    base_url, api_key = _VAULTS.get(vault, _VAULTS["cyberrange"])

    # Read note via Obsidian REST API
    safe_path = vault_path if vault_path.startswith("/") else f"/{vault_path}"
    req = urllib.request.Request(
        f"{base_url}/vault{urllib.parse.quote(safe_path)}",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return json.dumps({"error": f"Could not read note: {e}"})

    # Parse + chunk (inline minimal version)
    meta: dict = {}
    body = raw
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            try:
                import yaml
                meta = yaml.safe_load(raw[3:end]) or {}
            except Exception:
                pass
            body = raw[end+4:].lstrip("\n")

    title = str(meta.get("title", vault_path.split("/")[-1].replace(".md", "")))
    tags  = meta.get("tags", [])
    if isinstance(tags, str):
        tags = [tags]
    links = re.findall(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]", raw)
    file_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]

    # Chunk by heading
    lines = body.splitlines()
    chunks: list[dict] = []
    cur_heading = title
    cur_lines: list[str] = []

    def flush_chunk():
        text = "\n".join(cur_lines).strip()
        if len(text) >= 80:
            full = f"{title} — {cur_heading}\n\n{text}"
            chunks.append({"heading": cur_heading, "text": full[:1200]})

    for line in lines:
        if line.startswith("## ") or line.startswith("### "):
            flush_chunk()
            cur_heading = line.lstrip("#").strip()
            cur_lines = []
        else:
            cur_lines.append(line)
    flush_chunk()

    if not chunks:
        return json.dumps({"status": "skipped", "reason": "no meaningful chunks found"})

    # Embed + upsert
    from qdrant_client.models import PointStruct
    vecs = _embed([c["text"] for c in chunks])
    points = []
    for idx, (chunk, vec) in enumerate(zip(chunks, vecs)):
        point_id = int(hashlib.md5(f"{vault_path}::{idx}".encode()).hexdigest(), 16) % (2**63)
        points.append(PointStruct(
            id=point_id,
            vector=vec,
            payload={
                "path": vault_path, "title": title,
                "heading": chunk["heading"], "text": chunk["text"],
                "tags": tags, "links": links[:10], "file_hash": file_hash,
            },
        ))

    _qdrant().upsert(collection_name=COLL_NOTES, points=points, wait=True)
    return json.dumps({"status": "ok", "path": vault_path, "chunks_upserted": len(points)})


@mcp.tool
def collection_stats() -> str:
    """Return Qdrant collection statistics: point counts and index status."""
    stats = {}
    for coll in [COLL_NOTES, COLL_SENSORS, COLL_ANOMALY]:
        try:
            info_obj = _qdrant().get_collection(coll)
            stats[coll] = {
                "points":  info_obj.points_count,
                "status":  str(info_obj.status),
            }
        except Exception as e:
            stats[coll] = {"error": str(e)}
    return json.dumps(stats, indent=2)



@mcp.tool
def seed_sensor_topics(
    broker: str = "192.168.100.102",
    port: int = 31883,
    duration_s: int = 12,
) -> str:
    """
    Discover live MQTT topic paths and upsert them into the sensor_topics
    Qdrant collection. Subscribes to # on the MQTT broker for duration_s
    seconds, embeds each unique topic path, and stores it.

    Args:
        broker:     MQTT broker host (default: nandor NodePort 192.168.100.102)
        port:       MQTT port (default: 31883 NodePort)
        duration_s: How long to listen for topics (default 12 s)

    Returns:
        JSON with topics_found and points_upserted.
    """
    import time, threading
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        return json.dumps({"error": "pip install paho-mqtt"})
    from qdrant_client.models import PointStruct
    import hashlib

    seen: set[str] = set()
    done = threading.Event()

    def on_connect(c, *_):
        c.subscribe("#", qos=0)

    def on_message(c, userdata, msg):
        t = msg.topic
        if not t.startswith("$") and not t.startswith("spBv"):
            seen.add(t)

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                    client_id="qdrant-seed", clean_session=True,
                    protocol=mqtt.MQTTv311)
    c.on_connect = on_connect
    c.on_message = on_message
    try:
        c.connect(broker, port, keepalive=30)
        c.loop_start()
        time.sleep(duration_s)
        c.loop_stop()
        c.disconnect()
    except Exception as e:
        return json.dumps({"error": f"MQTT connect failed: {e}"})

    if not seen:
        return json.dumps({"status": "no topics found", "topics_found": 0})

    topics = sorted(seen)
    texts  = [f"MQTT sensor topic: {t}" for t in topics]
    vecs   = _embed(texts)

    points = []
    for topic, text, vec in zip(topics, texts, vecs):
        pid = int(hashlib.md5(topic.encode()).hexdigest(), 16) % (2**63)
        # derive a simple description from path segments
        parts = topic.split("/")
        points.append(PointStruct(
            id=pid, vector=vec,
            payload={"topic": topic, "text": text,
                     "namespace": "/".join(parts[:3]) if len(parts) >= 3 else topic}
        ))

    _qdrant().upsert(collection_name=COLL_SENSORS, points=points, wait=True)
    return json.dumps({"status": "ok", "topics_found": len(topics),
                       "points_upserted": len(points)})


@mcp.tool
def embed_vault(vault: str = "fde") -> str:
    """Re-index all Obsidian notes into Qdrant obsidian_notes collection.

    Walks the entire vault via the Obsidian REST API, re-embeds every note,
    and upserts to Qdrant. Old points for notes that no longer exist are NOT
    deleted (use Qdrant UI to drop/recreate the collection for a clean slate).

    vault: 'fde' (default) or 'cyberrange'
    """
    import urllib.request as _ur, urllib.parse as _up, json as _json

    _VAULTS = {
        "fde":        ("http://192.168.100.100:27123", "a9148b82dd5e61801a167c230613c3d468da1ba27a922180f31ff5416c98c5ef"),
        "cyberrange": (OBS_API_BASE, OBS_API_KEY),
    }
    base_url, api_key = _VAULTS.get(vault, _VAULTS["fde"])

    def _obs_get(path):
        req = _ur.Request(f"{base_url}{path}", headers={"Authorization": f"Bearer {api_key}"})
        with _ur.urlopen(req, timeout=30) as r:
            return _json.loads(r.read())

    def _list_all():
        data = _obs_get("/vault/")
        result = []
        def _recurse(entries, base=""):
            for e in entries.get("files", []):
                if e.endswith("/"):
                    sub = _obs_get(f"/vault/{_up.quote((base + e).strip('/'))}/")
                    _recurse(sub, base + e)
                elif e.endswith(".md"):
                    result.append((base + e).lstrip("/"))
        _recurse(data)
        return result

    notes = _list_all()
    total_chunks = 0
    errors = []

    for note_path in notes:
        try:
            result = _json.loads(ingest_note(note_path, vault=vault))
            if "chunks" in result:
                total_chunks += result["chunks"]
            elif "error" in result:
                errors.append(note_path)
        except Exception as e:
            errors.append(f"{note_path}: {e}")

    return _json.dumps({
        "status": "ok",
        "notes_processed": len(notes) - len(errors),
        "notes_errors": len(errors),
        "total_chunks_upserted": total_chunks,
        "errors": errors[:10],
    })

if __name__ == "__main__":

    import threading
    def _prewarm():
        try:
            _embed(["warmup"])
            _qdrant()
            logging.getLogger(__name__).warning("qdrant-mcp: pre-warm done")
        except Exception as e:
            logging.getLogger(__name__).warning("qdrant-mcp: pre-warm error %s", e)
    threading.Thread(target=_prewarm, daemon=True).start()
    mcp.run(transport="streamable-http", host="0.0.0.0", port=9103)
