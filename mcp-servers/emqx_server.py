#!/usr/bin/env python3
"""
EMQX 5.x REST API -> FastMCP server
Port: 9109 | Transport: streamable-http | Host: 0.0.0.0
"""

import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

os.environ["FASTMCP_DISABLE_BANNER"] = "1"

logging.basicConfig(level=logging.WARNING)

from fastmcp import FastMCP

# -- Config ------------------------------------------------------------------
EMQX_URL  = os.environ.get("EMQX_URL",  "http://127.0.0.1:32083")
EMQX_USER = os.environ.get("EMQX_USER", "admin")
EMQX_PASS = os.environ.get("EMQX_PASS", "fde-emqx-secret")

_token_cache: list[str] = [""]  # mutable single-element cache

mcp = FastMCP("emqx")


# -- Auth helper -------------------------------------------------------------
def _get_token() -> str:
    if _token_cache[0]:
        return _token_cache[0]
    url = f"{EMQX_URL}/api/v5/login"
    data = json.dumps({"username": EMQX_USER, "password": EMQX_PASS}).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            tok = json.loads(resp.read().decode()).get("token", "")
            _token_cache[0] = tok
            return tok
    except Exception as exc:
        return ""


# -- HTTP helper -------------------------------------------------------------
def _get(path: str) -> Any:
    """GET {EMQX_URL}/api/v5{path} with Bearer auth; return parsed JSON."""
    for attempt in range(2):
        token = _get_token()
        url = f"{EMQX_URL}/api/v5{path}"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0:
                _token_cache[0] = ""  # force re-login
                continue
            body = exc.read().decode(errors="replace")
            return {"error": f"HTTP {exc.code}", "detail": body}
        except Exception as exc:
            return {"error": str(exc)}
    return {"error": "auth_failed"}


# -- Tool 1: Broker stats ----------------------------------------------------
@mcp.tool()
def get_broker_stats() -> str:
    """
    Retrieve live broker metrics from EMQX: message rates, connection counts,
    subscription counts, and other key real-time indicators.
    """
    raw = _get("/monitor/current_metrics")
    if isinstance(raw, dict) and "error" in raw:
        return json.dumps(raw)

    want = {
        "connections.count", "connections.max",
        "live_connections.count",
        "subscriptions.count", "subscriptions.max",
        "topics.count", "topics.max",
        "retained.count",
        "messages.received", "messages.sent",
        "messages.dropped", "messages.publish",
        "bytes.received", "bytes.sent",
    }

    summary: dict[str, Any] = {}
    if isinstance(raw, list):
        for item in raw:
            k = item.get("key") or item.get("metric") or item.get("name", "")
            if k in want:
                summary[k] = item.get("value")
    elif isinstance(raw, dict):
        for k, v in raw.items():
            if k in want:
                summary[k] = v

    return json.dumps({"broker_stats": summary if summary else raw}, indent=2)


# -- Tool 2: Connected clients -----------------------------------------------
@mcp.tool()
def get_connected_clients(limit: int = 50) -> str:
    """
    Return a list of currently connected MQTT clients (up to `limit`).
    Each entry includes: clientid, ip_address, connected_at, subscriptions_cnt.
    """
    raw = _get(f"/clients?limit={limit}")
    if isinstance(raw, dict) and "error" in raw:
        return json.dumps(raw)

    items = raw if isinstance(raw, list) else raw.get("data", [])
    clients = []
    for c in items:
        entry: dict[str, Any] = {
            "clientid":          c.get("clientid"),
            "ip_address":        c.get("ip_address"),
            "connected_at":      c.get("connected_at"),
            "subscriptions_cnt": c.get("subscriptions_cnt"),
        }
        entry["recv_msg.qos0"] = (
            c["recv_msg"].get("qos0") if isinstance(c.get("recv_msg"), dict)
            else c.get("recv_msg.qos0")
        )
        entry["send_msg.qos0"] = (
            c["send_msg"].get("qos0") if isinstance(c.get("send_msg"), dict)
            else c.get("send_msg.qos0")
        )
        clients.append(entry)

    return json.dumps({"total": len(clients), "clients": clients}, indent=2)


# -- Tool 3: Subscriptions ---------------------------------------------------
@mcp.tool()
def list_subscriptions(topic_filter: str = "#") -> str:
    """
    List MQTT subscriptions matching `topic_filter` (default '#' = all).
    Returns which clients subscribe to which topics, including QoS.
    """
    encoded = urllib.parse.quote(topic_filter, safe="")
    raw = _get(f"/subscriptions?topic={encoded}&limit=100")
    if isinstance(raw, dict) and "error" in raw:
        return json.dumps(raw)

    items = raw if isinstance(raw, list) else raw.get("data", [])
    subs = [
        {"topic": s.get("topic"), "clientid": s.get("clientid"),
         "qos": s.get("qos"), "node": s.get("node")}
        for s in items
    ]
    return json.dumps({"total": len(subs), "subscriptions": subs}, indent=2)


# -- Tool 4: Retained messages -----------------------------------------------
@mcp.tool()
def get_retained_messages(topic: str = "#") -> str:
    """
    Fetch retained MQTT messages matching `topic` (default '#' = all).
    Payloads are base64-decoded and parsed as JSON or float where possible.
    """
    import base64
    encoded = urllib.parse.quote(topic, safe="")
    raw = _get(f"/mqtt/retainer/messages?topic={encoded}&limit=100")
    if isinstance(raw, dict) and "error" in raw:
        return json.dumps(raw)

    items = raw if isinstance(raw, list) else raw.get("data", [])

    def decode_payload(p: Any) -> Any:
        if not isinstance(p, str):
            return p
        try:
            decoded = base64.b64decode(p).decode("utf-8", errors="replace")
        except Exception:
            return p
        try:
            return json.loads(decoded)
        except Exception:
            pass
        try:
            return float(decoded)
        except Exception:
            pass
        return decoded

    messages = [
        {"topic": m.get("topic"), "qos": m.get("qos"),
         "publish_at": m.get("publish_at") or m.get("inserted_at"),
         "payload": decode_payload(m.get("payload"))}
        for m in items
    ]
    return json.dumps({"total": len(messages), "retained_messages": messages}, indent=2)


# -- Tool 5: Rule Engine rules -----------------------------------------------
@mcp.tool()
def list_rules() -> str:
    """
    List all Rule Engine rules configured in EMQX.
    Returns each rule's id, name, SQL, enabled/disabled status, and action count.
    """
    raw = _get("/rules")
    if isinstance(raw, dict) and "error" in raw:
        return json.dumps(raw)

    items = raw if isinstance(raw, list) else raw.get("data", [])
    rules = []
    for r in items:
        actions = r.get("actions", [])
        rules.append({
            "id":           r.get("id"),
            "name":         r.get("name"),
            "sql":          r.get("sql"),
            "enabled":      r.get("enable", r.get("enabled")),
            "description":  r.get("description", ""),
            "action_count": len(actions) if isinstance(actions, list) else actions,
        })
    return json.dumps({"total": len(rules), "rules": rules}, indent=2)


# -- Entry point -------------------------------------------------------------
if __name__ == "__main__":
    print(
        f"[emqx_server] Starting FastMCP on 0.0.0.0:9109  "
        f"EMQX={EMQX_URL}  user={EMQX_USER}",
        file=sys.stderr,
    )
    mcp.run(transport="streamable-http", host="0.0.0.0", port=9109)
