"""
predmaint-mcp  —  Predictive Maintenance MCP Server
nandor K8s · port 5100 · transport: streamable-http
metrics: port 9090 · /metrics (Prometheus)

Data:  TimeBase historian REST  (TIMEBASE_URL env)   ← primary
       MonsterMQ MQTT archive   (MONSTERMQ_MCP env)  ← fallback
ML:    ECOD (SOTA 2022, pyod) + IsolationForest ensemble
       Linear-regression degradation trend for RUL estimation
Alarms: published to EMQX MQTT broker
        topic: {UNS_GROUP}/{UNS_EDGE_NODE}/<area>/<line>/alarms/predmaint/<device>/<metric>
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

try:
    from pyod.models.ecod import ECOD
    from pyod.models.copod import COPOD
    HAS_PYOD = True
except ImportError:
    HAS_PYOD = False

try:
    import aiomqtt
    HAS_AIOMQTT = True
except ImportError:
    HAS_AIOMQTT = False

try:
    from prometheus_client import (
        Counter, Histogram, Gauge, start_http_server as _prom_start
    )
    HAS_PROMETHEUS = True
except ImportError:
    HAS_PROMETHEUS = False

os.environ["FASTMCP_DISABLE_BANNER"] = "1"
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
log = logging.getLogger("predmaint")

from fastmcp import FastMCP

mcp = FastMCP("predmaint")

# ── Config ───────────────────────────────────────────────────────────────────
MONSTERMQ_MCP          = os.getenv("MONSTERMQ_MCP", "http://10.200.0.11:3000/mcp")
TIMEBASE_URL           = os.getenv("TIMEBASE_URL", "http://10.200.0.11:4511")
TIMEBASE_DATASET       = os.getenv("TIMEBASE_DATASET", "DC")
ANOMALY_CONTAMINATION  = float(os.getenv("ANOMALY_CONTAMINATION", "0.05"))
HEALTH_WARN_THRESHOLD  = 70
HEALTH_CRIT_THRESHOLD  = 45

# UNS topic config — alarms are published under the UNS tree
UNS_GROUP     = os.getenv("UNS_GROUP", "dc")
UNS_EDGE_NODE = os.getenv("UNS_EDGE_NODE", "blans")

# MQTT alarm config
MQTT_BROKER         = os.getenv("MQTT_BROKER", "10.200.0.11")
MQTT_PORT           = int(os.getenv("MQTT_PORT", "1883"))
MQTT_ALARM_ENABLED  = os.getenv("MQTT_ALARM_ENABLED", "true").lower() == "true"
MQTT_ALARM_QOS      = int(os.getenv("MQTT_ALARM_QOS", "1"))
RUL_CRITICAL_HOURS  = float(os.getenv("RUL_CRITICAL_HOURS", "24.0"))
RUL_WARNING_HOURS   = float(os.getenv("RUL_WARNING_HOURS", "72.0"))

METRICS_PORT = int(os.getenv("METRICS_PORT", "9090"))

# ClickHouse watcher config
CLICKHOUSE_URL      = os.getenv(
    "CLICKHOUSE_URL",
    "http://fde-clickhouse-clickhouse.uns.svc.cluster.local:8123",
)
CLICKHOUSE_USER     = os.getenv("CLICKHOUSE_USER", "default")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "")
SCAN_INTERVAL_S = int(os.getenv("SCAN_INTERVAL_S", "300"))
SCAN_LOOKBACK_H = float(os.getenv("SCAN_LOOKBACK_H", "24.0"))
SCAN_ACTIVE_H   = float(os.getenv("SCAN_ACTIVE_H", "1.0"))

# ── Prometheus metrics ────────────────────────────────────────────────────────
if HAS_PROMETHEUS:
    _alarms_total = Counter(
        "predmaint_alarms_total", "Alarm state transitions",
        ["severity", "sensor"]
    )
    _analyze_duration = Histogram(
        "predmaint_analyze_duration_seconds", "analyze_sensor latency",
        buckets=[0.1, 0.5, 1, 2, 5, 10, 30]
    )
    _fit_duration = Histogram(
        "predmaint_fit_duration_seconds", "ML model fit latency",
        ["model"], buckets=[0.01, 0.05, 0.1, 0.5, 1, 5]
    )
    _active_alarms = Gauge("predmaint_active_alarms", "Currently active alarms")
    _analyses_total = Counter("predmaint_analyses_total", "Total analyze_sensor calls", ["status"])
else:
    class _Noop:
        def labels(self, **kw): return self
        def inc(self, *a): pass
        def observe(self, *a): pass
        def set(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
    _alarms_total = _Noop()
    _analyze_duration = _Noop()
    _fit_duration = _Noop()
    _active_alarms = _Noop()
    _analyses_total = _Noop()

# ── UNS alarm topic builder ───────────────────────────────────────────────────

def _alarm_topic(sensor_topic: str) -> str:
    """
    Build a UNS-shaped MQTT alarm topic from a sensor topic.

    Input:  dc/blans/kill/clean/stamp-1/ink_pct
    Output: dc/blans/kill/clean/alarms/predmaint/stamp-1/ink_pct

    Inserts 'alarms/predmaint' after the 4th path segment (line level) when the
    topic starts with {UNS_GROUP}/{UNS_EDGE_NODE}/.  Falls back to
    {group}/{edge}/alarms/predmaint/{full_topic} for unrecognised structures.
    """
    parts = sensor_topic.split("/")
    if len(parts) >= 4 and parts[0] == UNS_GROUP and parts[1] == UNS_EDGE_NODE:
        return "/".join(parts[:4] + ["alarms", "predmaint"] + parts[4:])
    return f"{UNS_GROUP}/{UNS_EDGE_NODE}/alarms/predmaint/{sensor_topic}"

# ── MonsterMQ client ─────────────────────────────────────────────────────────
_client: httpx.AsyncClient | None = None

def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=15.0)
    return _client

async def mmq(tool: str, args: dict, timeout: float = 3.0) -> Any:
    """Call a MonsterMQ MCP tool via JSON-RPC over HTTP."""
    try:
        r = await _http().post(
            MONSTERMQ_MCP,
            json={"jsonrpc": "2.0", "method": "tools/call", "id": 1,
                  "params": {"name": tool, "arguments": args}},
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"},
            timeout=timeout,
        )
        data = r.json()
        content = data.get("result", {}).get("content", [])
        if content and content[0].get("type") == "text":
            txt = content[0]["text"]
            try:
                return json.loads(txt)
            except json.JSONDecodeError:
                return txt
        return data
    except Exception as e:
        return {"error": str(e)}

# ── TimeBase historian client ─────────────────────────────────────────────────

import urllib.parse as _urlparse

async def _tb_fetch_raw(topic: str, hours: float) -> list[dict]:
    """Fetch raw data points from TimeBase REST API for a single tag."""
    tag = topic
    path = f"/api/datasets/{_urlparse.quote(TIMEBASE_DATASET, safe='')}/data/{_urlparse.quote(tag, safe='')}"
    url  = f"{TIMEBASE_URL}{path}?relativeStart=-{int(max(hours, 1))}h"
    try:
        r = await _http().get(url, headers={"Accept": "application/json"}, timeout=2.0)
        d = r.json()
        return d.get("d", [])
    except Exception:
        return []

def _parse_tb_points(points: list[dict]) -> tuple["np.ndarray", list[float]]:
    """Extract (values, unix_timestamps) from TimeBase data points."""
    values, timestamps = [], []
    for pt in points:
        v = pt.get("v")
        t = pt.get("t")
        if v is None or t is None:
            continue
        try:
            fv = float(v)
            ft = datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
            values.append(fv)
            timestamps.append(ft)
        except (TypeError, ValueError):
            continue
    if not values:
        return np.array([]), []
    return np.array(values, dtype=float), timestamps

async def fetch_history(
    topic: str,
    hours: float,
    interval: str = "5m",
    archive_group: str = "Default",
) -> tuple["np.ndarray", list[float]]:
    """ClickHouse-only history fetch."""
    return await _ch_fetch_history(topic, hours, interval)


async def _ch_fetch_history(
    topic: str, hours: float, interval: str = "5m"
) -> "tuple[np.ndarray, list[float]]":
    """Fetch aggregated history from ClickHouse — fallback when TimeBase/MonsterMQ lack data."""
    secs = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}.get(interval, 300)
    safe = topic.replace("'", "''")
    sql = (
        f"SELECT toUnixTimestamp(toStartOfInterval(ts, INTERVAL {secs} SECOND)) AS t,"
        f" avg(value_num) AS v"
        f" FROM uns.sensor_raw"
        f" WHERE topic = '{safe}'"
        f" AND ts >= now() - INTERVAL {int(hours)} HOUR"
        f" AND value_num IS NOT NULL"
        f" GROUP BY t ORDER BY t FORMAT JSONEachRow"
    )
    url = f"{CLICKHOUSE_URL}/?query={_urlparse.quote(sql)}"
    try:
        r = await _http().get(url, auth=(CLICKHOUSE_USER, CLICKHOUSE_PASSWORD), timeout=30.0)
        rows = [json.loads(line) for line in r.text.splitlines() if line.strip()]
        if not rows:
            return np.array([]), []
        values     = np.array([float(row["v"]) for row in rows], dtype=float)
        timestamps = [float(row["t"]) for row in rows]
        return values, timestamps
    except Exception:
        return np.array([]), []


async def _ch_get_latest(topic: str) -> dict:
    """Return the most-recent row for a topic from ClickHouse."""
    safe = topic.replace("'", "''")
    sql = (
        f"SELECT ts, value_num, value_str FROM uns.sensor_raw"
        f" WHERE topic = '{safe}'"
        f" ORDER BY ts DESC LIMIT 1 FORMAT JSONEachRow"
    )
    url = f"{CLICKHOUSE_URL}/?query={_urlparse.quote(sql)}"
    try:
        r = await _http().get(url, auth=(CLICKHOUSE_USER, CLICKHOUSE_PASSWORD), timeout=5.0)
        rows = [json.loads(line) for line in r.text.splitlines() if line.strip()]
        if rows:
            row = rows[0]
            return {"topic": topic, "source": "clickhouse",
                    "ts": row["ts"],
                    "value": row.get("value_num") if row.get("value_num") is not None else row.get("value_str")}
        return {"error": f"no_data for {topic}"}
    except Exception as e:
        return {"error": str(e)}


# ── MQTT alarm publisher ──────────────────────────────────────────────────────

_alarm_state: dict[str, str] = {}
_alarm_queue: asyncio.Queue | None = None
_mqtt_publisher_task: asyncio.Task | None = None


async def _mqtt_publisher_loop() -> None:
    """Background task: drain _alarm_queue over a persistent MQTT connection."""
    global _alarm_queue
    assert _alarm_queue is not None
    while True:
        if not HAS_AIOMQTT:
            await asyncio.sleep(60)
            continue
        try:
            async with aiomqtt.Client(
                hostname=MQTT_BROKER,
                port=MQTT_PORT,
                identifier="predmaint-alarm",
                keepalive=30,
            ) as client:
                log.warning(f"MQTT alarm publisher connected to {MQTT_BROKER}:{MQTT_PORT}")
                while True:
                    try:
                        item = await asyncio.wait_for(_alarm_queue.get(), timeout=25.0)
                    except asyncio.TimeoutError:
                        continue
                    await client.publish(
                        item["topic"],
                        payload=item["payload"],
                        qos=MQTT_ALARM_QOS,
                        retain=True,
                    )
                    log.warning(f"MQTT alarm → {item['topic']}  [{item['severity']}]")
                    _alarm_queue.task_done()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.warning(f"MQTT alarm publisher error: {exc!r} — reconnecting in 15 s")
            await asyncio.sleep(15)


def _ensure_mqtt_publisher() -> None:
    global _alarm_queue, _mqtt_publisher_task
    if not MQTT_ALARM_ENABLED:
        return
    if _alarm_queue is None:
        _alarm_queue = asyncio.Queue()
    if _mqtt_publisher_task is None or _mqtt_publisher_task.done():
        _mqtt_publisher_task = asyncio.create_task(_mqtt_publisher_loop())


def _effective_severity(health: dict, rul: dict) -> str:
    sev = health.get("severity", "OK")
    rul_h = rul.get("rul_hours")
    if rul_h is not None:
        if rul_h < RUL_CRITICAL_HOURS:
            return "CRITICAL"
        if rul_h < RUL_WARNING_HOURS and sev == "OK":
            return "WARNING"
    return sev


async def _maybe_publish_alarm(
    sensor_topic: str,
    health: dict,
    rul: dict,
    recommendation: str,
) -> None:
    if not MQTT_ALARM_ENABLED:
        return

    severity = _effective_severity(health, rul)
    last = _alarm_state.get(sensor_topic)

    if severity == last:
        return
    if severity == "OK" and last is None:
        return

    _alarm_state[sensor_topic] = severity
    _ensure_mqtt_publisher()

    # UNS-shaped alarm topic (alarms cell inserted at line level)
    alarm_topic = _alarm_topic(sensor_topic)
    payload = json.dumps({
        "source":        "predmaint",
        "sensor":        sensor_topic,
        "severity":      severity,
        "active":        severity != "OK",
        "health_index":  health.get("health_index"),
        "trend":         health.get("trend"),
        "anomaly_rate":  health.get("anomaly_rate"),
        "rul_hours":     rul.get("rul_hours"),
        "rul_days":      rul.get("rul_days"),
        "recommendation": recommendation,
        "timestamp":     datetime.now(timezone.utc).isoformat(),
    })

    _alarms_total.labels(severity=severity, sensor=sensor_topic).inc()
    _active_alarms.set(sum(1 for s in _alarm_state.values() if s not in ("OK", None)))

    if _alarm_queue is not None:
        await _alarm_queue.put({"topic": alarm_topic, "payload": payload, "severity": severity})


# ── ML engine ─────────────────────────────────────────────────────────────────

def _if_scores(X: np.ndarray) -> np.ndarray:
    if len(X) < 10:
        return np.zeros(len(X))
    t0 = time.time()
    Xs = StandardScaler().fit_transform(X.reshape(-1, 1))
    clf = IsolationForest(contamination=ANOMALY_CONTAMINATION, random_state=42, n_jobs=1)
    clf.fit(Xs)
    _fit_duration.labels(model="IsolationForest").observe(time.time() - t0)
    raw = -clf.decision_function(Xs)
    lo, hi = raw.min(), raw.max()
    return (raw - lo) / (hi - lo + 1e-10)

def _ecod_scores(X: np.ndarray) -> np.ndarray:
    if not HAS_PYOD or len(X) < 10:
        return _if_scores(X)
    try:
        t0 = time.time()
        clf = ECOD(contamination=ANOMALY_CONTAMINATION)
        clf.fit(X.reshape(-1, 1).astype(float))
        _fit_duration.labels(model="ECOD").observe(time.time() - t0)
        s = clf.decision_scores_
        return s / (s.max() + 1e-10)
    except Exception:
        return _if_scores(X)

def _health_index(values: np.ndarray) -> dict:
    if len(values) < 4:
        return {"health_index": 100, "severity": "OK", "trend": "insufficient_data",
                "recent_anomalies": 0, "anomaly_rate": 0.0, "slope": 0.0}

    ecod = _ecod_scores(values)
    ifs  = _if_scores(values)
    ensemble = (ecod + ifs) / 2.0

    recent_n  = max(1, len(values) // 5)
    rec_scores = ensemble[-recent_n:]
    rec_anomalies = int((rec_scores > 0.6).sum())
    anomaly_rate  = float(rec_anomalies / recent_n)

    norm  = (values - values.mean()) / (values.std() + 1e-10)
    t     = np.arange(len(norm), dtype=float)
    slope = float(np.polyfit(t, norm, 1)[0]) if len(t) > 1 else 0.0
    trend = ("degrading" if slope > 0.02 else
             "improving" if slope < -0.02 else "stable")

    if len(values) >= 20:
        vol = np.std(values[-recent_n:]) / (np.std(values[:-recent_n]) + 1e-10)
    else:
        vol = 1.0

    hi  = 100.0
    hi -= anomaly_rate * 40
    hi -= min(abs(slope) * 200, 20)
    hi -= min(max(vol - 1, 0) * 15, 15)
    hi -= float(rec_scores.mean()) * 25
    hi  = float(np.clip(hi, 0, 100))

    sev = ("CRITICAL" if hi < HEALTH_CRIT_THRESHOLD else
           "WARNING"  if hi < HEALTH_WARN_THRESHOLD else "OK")

    return {
        "health_index":     round(hi, 1),
        "severity":         sev,
        "trend":            trend,
        "recent_anomalies": rec_anomalies,
        "anomaly_rate":     round(anomaly_rate, 3),
        "slope":            round(slope, 5),
    }

def _rul(values: np.ndarray, timestamps: list[float]) -> dict:
    if len(values) < 8:
        return {"rul_hours": None, "confidence": "low", "reason": "insufficient_data"}

    t      = (np.array(timestamps) - timestamps[0]) / 3600.0
    coeffs = np.polyfit(t, values, 1)
    slope, intercept = float(coeffs[0]), float(coeffs[1])

    if abs(slope) < 1e-10:
        return {"rul_hours": None, "confidence": "low", "reason": "no_trend"}

    mu, sigma = values.mean(), values.std()
    threshold = mu + 3 * sigma if slope > 0 else mu - 3 * sigma
    rul_h     = (threshold - values[-1]) / slope

    pred  = np.polyval(coeffs, t)
    ss_r  = np.sum((values - pred) ** 2)
    ss_t  = np.sum((values - mu) ** 2)
    r2    = float(1 - ss_r / (ss_t + 1e-10))
    conf  = "high" if r2 > 0.7 else ("medium" if r2 > 0.4 else "low")

    if rul_h <= 0:
        return {"rul_hours": None, "confidence": conf,
                "reason": "threshold_already_exceeded", "r2": round(r2, 3)}
    if rul_h > 8760:
        return {"rul_hours": None, "confidence": conf,
                "reason": "trend_too_slow", "r2": round(r2, 3)}

    return {
        "rul_hours": round(rul_h, 1),
        "rul_days":  round(rul_h / 24, 1),
        "confidence": conf,
        "r2":         round(r2, 3),
        "direction":  "increasing" if slope > 0 else "decreasing",
    }

def _recommend(health: dict, rul: dict, topic: str) -> str:
    sev   = health.get("severity", "OK")
    trend = health.get("trend", "stable")
    rul_h = rul.get("rul_hours")

    if sev == "CRITICAL":
        msg = f"CRITICAL — immediate inspection required."
        if rul_h:
            msg += f" Estimated failure in {rul_h:.0f}h."
        return msg
    if sev == "WARNING":
        msg = "Schedule preventive maintenance within 48h."
        if trend == "degrading":
            msg += " Trend is actively degrading."
        return msg
    if trend == "degrading":
        return "OK but degrading — monitor closely, plan inspection."
    return "Operating within normal parameters."

# ── MonsterMQ helpers ─────────────────────────────────────────────────────────

_INTERVAL_MAP: dict[str, str] = {
    "1m":  "ONE_MINUTE",
    "5m":  "FIVE_MINUTES",
    "15m": "FIFTEEN_MINUTES",
    "30m": "FIFTEEN_MINUTES",
    "1h":  "ONE_HOUR",
    "1d":  "ONE_DAY",
    "ONE_MINUTE":    "ONE_MINUTE",
    "FIVE_MINUTES":  "FIVE_MINUTES",
    "FIFTEEN_MINUTES": "FIFTEEN_MINUTES",
    "ONE_HOUR":      "ONE_HOUR",
    "ONE_DAY":       "ONE_DAY",
}

def _mmq_interval(interval: str) -> str:
    return _INTERVAL_MAP.get(interval, "FIVE_MINUTES")


# ── Data helpers ──────────────────────────────────────────────────────────────

def _parse_history(raw: Any) -> tuple[np.ndarray, list[float]]:
    rows: list[dict] = []
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        for key in ("data", "messages", "results", "items"):
            if key in raw and isinstance(raw[key], list):
                rows = raw[key]
                break

    values, timestamps = [], []
    for row in rows:
        if not isinstance(row, dict):
            continue
        v = row.get("value") or row.get("avg") or row.get("mean") or row.get("payload")
        t = row.get("timestamp") or row.get("time") or row.get("ts")
        if v is None or t is None:
            continue
        try:
            fv = float(v)
            ft = (float(t) if isinstance(t, (int, float)) else
                  datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp())
            values.append(fv)
            timestamps.append(ft)
        except (TypeError, ValueError):
            continue

    if not values:
        return np.array([]), []
    arr = np.array(values, dtype=float)
    return arr, timestamps

# ── MCP tools ─────────────────────────────────────────────────────────────────

async def _ch_find_topics(
    pattern: str = "",
    hours: float = 24.0,
    limit: int = 200,
) -> list[str]:
    """Return distinct numeric topics active in the last `hours` hours."""
    safe = pattern.replace("'", "''").replace("#", "%").replace("+", "_")
    where_pat = f" AND topic ILIKE '%{safe}%'" if safe and safe.strip("%") else ""
    sql = (
        f"SELECT DISTINCT topic FROM uns.sensor_raw"
        f" WHERE ts >= now() - INTERVAL {int(hours)} HOUR"
        f" AND value_num IS NOT NULL"
        f"{where_pat}"
        f" LIMIT {limit} FORMAT JSONEachRow"
    )
    url = f"{CLICKHOUSE_URL}/?query={_urlparse.quote(sql)}"
    try:
        r = await _http().get(url, auth=(CLICKHOUSE_USER, CLICKHOUSE_PASSWORD), timeout=30.0)
        return [json.loads(line)["topic"] for line in r.text.splitlines() if line.strip()]
    except Exception:
        return []


@mcp.tool
async def list_sensors(
    topic_filter: str = "",
    hours: float = 24.0,
) -> str:
    """
    List active sensor topics from ClickHouse uns.sensor_raw.
    topic_filter: optional substring / LIKE pattern (# or % wildcard).
    hours: lookback for "active" (default 24 h).
    Returns up to 200 matching topics ordered by message count.
    """
    safe = topic_filter.replace("'", "''").replace("#", "%")
    where_pat = f" AND topic ILIKE '%{safe}%'" if safe else ""
    sql = (
        f"SELECT topic, count() AS cnt, max(ts) AS last_seen"
        f" FROM uns.sensor_raw"
        f" WHERE ts >= now() - INTERVAL {int(hours)} HOUR"
        f" AND value_num IS NOT NULL"
        f"{where_pat}"
        f" GROUP BY topic ORDER BY cnt DESC LIMIT 200 FORMAT JSONEachRow"
    )
    url = f"{CLICKHOUSE_URL}/?query={_urlparse.quote(sql)}"
    try:
        r = await _http().get(url, auth=(CLICKHOUSE_USER, CLICKHOUSE_PASSWORD), timeout=30.0)
        rows = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    except Exception as e:
        return json.dumps({"error": str(e)})
    return json.dumps({
        "source": "clickhouse",
        "count": len(rows),
        "hours": hours,
        "topics": [{"topic": row["topic"], "count": int(row["cnt"]), "last_seen": row["last_seen"]} for row in rows],
    }, indent=2)


@mcp.tool
async def find_sensors(
    query: str,
    top_n: int = 10,
    hours: float = 24.0,
) -> str:
    """
    Find sensor topics by keyword matching against active ClickHouse topics.
    Each word in query is matched as a substring of the topic path.

    Args:
        query:  keywords, e.g. "reflow temp" or "conveyor current"
        top_n:  how many matches to return (default 10)
        hours:  lookback for active sensors (default 24 h)
    """
    import re as _re
    tokens = [t for t in _re.split(r"[\s,]+", query.lower()) if len(t) >= 2]
    all_topics = await _ch_find_topics(hours=hours, limit=500)
    scored = []
    for t in all_topics:
        tl = t.lower()
        score = sum(1 for tok in tokens if tok in tl)
        if score > 0:
            scored.append({"topic": t, "score": score})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return json.dumps({
        "source":  "clickhouse",
        "query":   query,
        "tokens":  tokens,
        "matches": len(scored),
        "results": scored[:top_n],
    }, indent=2)

@mcp.tool
async def get_sensor_now(topic: str) -> str:
    """Return the current / most-recent value for a sensor topic from ClickHouse."""
    result = await _ch_get_latest(topic)
    return json.dumps(result, indent=2)

@mcp.tool
async def analyze_sensor(
    topic: str,
    hours: float = 24.0,
    interval: str = "5m",
) -> str:
    """
    Full predictive-maintenance analysis on a single sensor.

    Fetches aggregated history from ClickHouse, runs ECOD + IsolationForest
    ensemble anomaly detection, computes a 0-100 health index, and estimates
    RUL. Publishes MQTT alarm to EMQX if severity changes.

    Args:
        topic:    sensor_raw topic path
        hours:    lookback window (default 24 h)
        interval: aggregation bucket — 1m | 5m | 15m | 30m | 1h
    """
    t0 = time.time()
    try:
        values, timestamps = await _ch_fetch_history(topic, hours, interval)

        if len(values) == 0:
            _analyses_total.labels(status="no_data").inc()
            return json.dumps({
                "topic":   topic,
                "status":  "no_data",
                "message": f"No data for '{topic}' in last {hours}h.",
            })

        health  = _health_index(values)
        rul     = _rul(values, timestamps)
        current = await _ch_get_latest(topic)
        rec     = _recommend(health, rul, topic)

        await _maybe_publish_alarm(topic, health, rul, rec)
        _analyses_total.labels(status="ok").inc()

        return json.dumps({
            "topic":        topic,
            "source":       "clickhouse",
            "window_hours": hours,
            "data_points":  len(values),
            "current":      current,
            "statistics": {
                "mean": round(float(values.mean()), 4),
                "std":  round(float(values.std()),  4),
                "min":  round(float(values.min()),  4),
                "max":  round(float(values.max()),  4),
                "last": round(float(values[-1]),    4),
            },
            "health":        health,
            "rul":           rul,
            "recommendation":rec,
            "ml_engine":     "ECOD+IF ensemble" if HAS_PYOD else "IF+Zscore",
            "alarm_topic":   _alarm_topic(topic),
            "alarm_active":  _alarm_state.get(topic) not in (None, "OK"),
            "generated_at":  datetime.now(timezone.utc).isoformat(),
        }, indent=2)
    except Exception:
        _analyses_total.labels(status="error").inc()
        raise
    finally:
        _analyze_duration.observe(time.time() - t0)

@mcp.tool
async def analyze_asset(
    asset_prefix: str,
    hours: float = 24.0,
    interval: str = "15m",
    max_sensors: int = 8,
) -> str:
    """
    Analyze all sensors whose topic starts with asset_prefix, from ClickHouse.

    Args:
        asset_prefix: topic prefix, e.g. 'spBv1.0/PCBFactory/DDATA/spark-18ce/ReflowOven'
        hours:        lookback window
        max_sensors:  cap to avoid timeout (default 8)
    """
    topics = await _ch_find_topics(pattern=asset_prefix + "%", hours=hours, limit=max_sensors)
    if not topics:
        return json.dumps({"asset": asset_prefix, "source": "clickhouse", "status": "no_sensors_found"})

    results = []
    for t in topics[:max_sensors]:
        raw = await analyze_sensor(t, hours, interval)
        try:
            results.append(json.loads(raw))
        except json.JSONDecodeError:
            results.append({"topic": t, "error": "parse_error"})

    hi_scores  = [r["health"]["health_index"] for r in results if "health" in r]
    severities = [r["health"]["severity"]     for r in results if "health" in r]
    n_crit = sum(1 for s in severities if s == "CRITICAL")
    n_warn = sum(1 for s in severities if s == "WARNING")
    ruls   = [r["rul"]["rul_hours"] for r in results if r.get("rul", {}).get("rul_hours") is not None]

    return json.dumps({
        "asset":                asset_prefix,
        "source":               "clickhouse",
        "sensors_analyzed":     len(results),
        "overall_health_index": round(min(hi_scores), 1) if hi_scores else 100,
        "overall_severity":     "CRITICAL" if n_crit else "WARNING" if n_warn else "OK",
        "critical_sensors":     n_crit,
        "warning_sensors":      n_warn,
        "min_rul_hours":        round(min(ruls), 1) if ruls else None,
        "min_rul_days":         round(min(ruls) / 24, 1) if ruls else None,
        "sensors":              results,
        "generated_at":         datetime.now(timezone.utc).isoformat(),
    }, indent=2)

@mcp.tool
async def detect_anomalies(
    topic_filter: str = "",
    hours: float = 6.0,
    min_severity: str = "WARNING",
    max_topics: int = 20,
) -> str:
    """
    Scan topics matching a filter from ClickHouse and report anomalies.

    Args:
        topic_filter: substring filter on topic path (e.g. 'PCBFactory', 'ReflowOven')
        hours:        lookback window (default 6 h)
        min_severity: WARNING | CRITICAL
        max_topics:   max topics to scan (default 20)
    """
    topics = await _ch_find_topics(pattern=topic_filter, hours=hours, limit=max_topics)

    anomalies = []
    for t in topics:
        raw = await analyze_sensor(t, hours, "15m")
        try:
            r = json.loads(raw)
            if r.get("status") == "no_data":
                continue
            h   = r.get("health", {})
            sev = h.get("severity", "OK")
            if sev == "OK":
                continue
            if sev == "WARNING" and min_severity == "CRITICAL":
                continue
            anomalies.append({
                "topic":            t,
                "alarm_topic":      _alarm_topic(t),
                "severity":         sev,
                "health_index":     h.get("health_index"),
                "recent_anomalies": h.get("recent_anomalies", 0),
                "trend":            h.get("trend"),
                "rul_hours":        r.get("rul", {}).get("rul_hours"),
                "recommendation":   r.get("recommendation"),
            })
        except (json.JSONDecodeError, KeyError):
            continue

    anomalies.sort(key=lambda x: x.get("health_index", 100))

    return json.dumps({
        "source":          "clickhouse",
        "scanned_topics":  len(topics),
        "anomalies_found": len(anomalies),
        "min_severity":    min_severity,
        "window_hours":    hours,
        "anomalies":       anomalies,
        "generated_at":    datetime.now(timezone.utc).isoformat(),
    }, indent=2)

@mcp.tool
async def maintenance_recommendations(
    topic_filter: str = "spBv1.0/%/DDATA/%",
    max_sensors: int = 30,
) -> str:
    """
    Scan active ClickHouse sensors, group by asset, return prioritised P0-P2 list.

    P0 = RUL < 48 h (failure imminent)
    P1 = CRITICAL health index
    P2 = WARNING health index

    Args:
        topic_filter: SQL LIKE pattern (default: all SpB DDATA topics)
        max_sensors:  max sensors to analyze (default 30)
    """
    topics = await _ch_find_topics(pattern=topic_filter, hours=12.0, limit=max_sensors)

    asset_map: dict[str, list[str]] = defaultdict(list)
    for t in topics:
        parts = t.split("/")
        key = "/".join(parts[:5]) if len(parts) >= 5 else "/".join(parts[:3])
        asset_map[key].append(t)

    recs = []
    for asset, atopics in list(asset_map.items())[:10]:
        for t in atopics[:4]:
            raw = await analyze_sensor(t, 12.0, "30m")
            try:
                r   = json.loads(raw)
                h   = r.get("health", {})
                rul = r.get("rul", {})
                rul_h = rul.get("rul_hours")
                sev   = h.get("severity", "OK")

                if rul_h is not None and rul_h < 48:
                    recs.append({"priority": "P0", "asset": asset, "topic": t,
                                 "alarm_topic": _alarm_topic(t),
                                 "issue": f"RUL {rul_h:.0f}h — failure imminent",
                                 "health_index": h.get("health_index"),
                                 "action": "Immediate maintenance required"})
                elif sev == "CRITICAL":
                    recs.append({"priority": "P1", "asset": asset, "topic": t,
                                 "alarm_topic": _alarm_topic(t),
                                 "issue": f"Health {h.get('health_index')} CRITICAL",
                                 "health_index": h.get("health_index"),
                                 "action": r.get("recommendation", "Inspect and service")})
                elif sev == "WARNING":
                    recs.append({"priority": "P2", "asset": asset, "topic": t,
                                 "alarm_topic": _alarm_topic(t),
                                 "issue": f"Health {h.get('health_index')} WARNING",
                                 "health_index": h.get("health_index"),
                                 "action": r.get("recommendation", "Schedule inspection")})
            except (json.JSONDecodeError, KeyError):
                continue

    recs.sort(key=lambda x: (x.get("priority", "P9"), x.get("health_index", 100)))

    return json.dumps({
        "source":          "clickhouse",
        "assets_scanned":  len(asset_map),
        "recommendations": recs,
        "generated_at":    datetime.now(timezone.utc).isoformat(),
    }, indent=2)

@mcp.tool
async def query_history(
    topic: str,
    hours: float = 1.0,
    interval: str = "1m",
) -> str:
    """
    Raw aggregated time-series from ClickHouse — no ML.
    Returns timestamped avg values for trending, charts, or custom analysis.

    Args:
        topic:    sensor_raw topic path
        hours:    lookback window (default 1 h)
        interval: aggregation bucket — 1m | 5m | 15m | 30m | 1h
    """
    values, timestamps = await _ch_fetch_history(topic, hours, interval)
    if len(values) == 0:
        return json.dumps({"error": "no data", "topic": topic, "hours": hours})
    rows = [{"timestamp": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
             "value": round(float(v), 6)} for v, ts in zip(values.tolist(), timestamps)]
    return json.dumps({"topic": topic, "source": "clickhouse",
                       "points": len(rows), "data": rows}, indent=2)

@mcp.tool
async def alarm_status() -> str:
    """
    Return the current in-process alarm state for all sensors that have been
    analyzed since the service started.
    """
    active   = {t: s for t, s in _alarm_state.items() if s != "OK"}
    cleared  = {t: s for t, s in _alarm_state.items() if s == "OK"}
    publisher_running = (
        _mqtt_publisher_task is not None and not _mqtt_publisher_task.done()
    )
    queue_depth = _alarm_queue.qsize() if _alarm_queue else 0

    return json.dumps({
        "mqtt_alarm_enabled":       MQTT_ALARM_ENABLED,
        "mqtt_broker":              f"{MQTT_BROKER}:{MQTT_PORT}",
        "aiomqtt_available":        HAS_AIOMQTT,
        "publisher_running":        publisher_running,
        "queue_depth":              queue_depth,
        "alarm_topic_scheme":       f"{UNS_GROUP}/{UNS_EDGE_NODE}/<area>/<line>/alarms/predmaint/<device>/<metric>",
        "uns_group":                UNS_GROUP,
        "uns_edge_node":            UNS_EDGE_NODE,
        "active_alarms":            active,
        "cleared_sensors":          cleared,
        "total_tracked_sensors":    len(_alarm_state),
        "rul_critical_hours":       RUL_CRITICAL_HOURS,
        "rul_warning_hours":        RUL_WARNING_HOURS,
    }, indent=2)


@mcp.tool
async def service_info() -> str:
    """Return service metadata: version, ML backend, data-source reachability."""
    ch_ok = True
    ch_rows = 0
    try:
        sql = "SELECT count() as c FROM uns.sensor_raw WHERE ts >= now() - INTERVAL 1 HOUR FORMAT JSONEachRow"
        url = f"{CLICKHOUSE_URL}/?query={_urlparse.quote(sql)}"
        r = await _http().get(url, auth=(CLICKHOUSE_USER, CLICKHOUSE_PASSWORD), timeout=5.0)
        ch_rows = int(json.loads(r.text.strip()).get("c", 0)) if r.text.strip() else 0
    except Exception:
        ch_ok = False
    return json.dumps({
        "service":            "predmaint-mcp",
        "version":            "1.3.0",
        "deployment":         "nandor K8s / uns",
        "ml_backend":         "ECOD+IsolationForest (pyod)" if HAS_PYOD else "IsolationForest+Zscore (sklearn)",
        "pyod":               HAS_PYOD,
        "data_source":        "clickhouse",
        "clickhouse_url":     CLICKHOUSE_URL,
        "clickhouse_ok":      ch_ok,
        "clickhouse_rows_1h": ch_rows,
        "mqtt_broker":        f"{MQTT_BROKER}:{MQTT_PORT}",
        "mqtt_alarms":        MQTT_ALARM_ENABLED,
        "aiomqtt":            HAS_AIOMQTT,
        "prometheus_metrics": HAS_PROMETHEUS,
        "metrics_port":       METRICS_PORT,
        "alarm_topic_scheme": f"{UNS_GROUP}/{UNS_EDGE_NODE}/<area>/<device>/alarms/predmaint/<metric>",
        "tools": [
            "list_sensors", "find_sensors", "get_sensor_now", "analyze_sensor",
            "analyze_asset", "detect_anomalies",
            "maintenance_recommendations", "query_history",
            "alarm_status", "service_info",
        ],
    }, indent=2)



# ── Continuous sensor_raw watcher ────────────────────────────────────────────

async def _watcher_ch_query(client: "httpx.AsyncClient", sql: str) -> list[dict]:
    """Run a ClickHouse JSONCompact query and return list of row dicts."""
    import urllib.parse as _up
    url = (
        f"{CLICKHOUSE_URL}/"
        f"?user={CLICKHOUSE_USER}"
        f"&password={_up.quote(CLICKHOUSE_PASSWORD)}"
        f"&query={_up.quote(sql + ' FORMAT JSONCompact')}"
    )
    try:
        r = await client.get(url, timeout=30.0)
        r.raise_for_status()
        d = r.json()
        cols = [c["name"] for c in d.get("meta", [])]
        return [dict(zip(cols, row)) for row in d.get("data", [])]
    except Exception as exc:
        log.warning(f"CH query error: {exc}")
        return []


async def _watcher_list_sensors(client: "httpx.AsyncClient") -> list[dict]:
    """Return distinct active sensors from sensor_raw (last SCAN_ACTIVE_H hours)."""
    sql = (
        "SELECT DISTINCT topic FROM uns.sensor_raw "
        "WHERE topic LIKE 'spBv1.0/%/DDATA/%' "
        f"AND ts > now() - INTERVAL {int(SCAN_ACTIVE_H)} HOUR "
        "AND value_num IS NOT NULL "
        "LIMIT 300"
    )
    rows = await _watcher_ch_query(client, sql)
    sensors = []
    for row in rows:
        topic = row["topic"]
        parts = topic.split("/")
        # spBv1.0 / dc-dk-blans / DDATA / <area> / <device> / <metric...>
        if len(parts) < 6:
            continue
        area   = parts[3]
        device = parts[4]
        metric = "/".join(parts[5:])
        sensors.append({
            "spb_topic":    topic,
            "cell":         device,
            "tag":          metric,
            "sensor_topic": f"{UNS_GROUP}/{UNS_EDGE_NODE}/{area}/{device}/{metric}",
        })
    return sensors


async def _watcher_fetch_history(
    client: "httpx.AsyncClient", spb_topic: str
) -> "tuple[np.ndarray, list[float]]":
    """Fetch 5-minute aggregated history from sensor_raw for a full SpB topic."""
    safe = spb_topic.replace("'", "''")
    sql = (
        "SELECT "
        "toUnixTimestamp(toStartOfInterval(ts, INTERVAL 5 MINUTE)) * 1000 AS t, "
        "avg(value_num) AS v "
        "FROM uns.sensor_raw "
        f"WHERE topic = '{safe}' "
        f"AND ts > now() - INTERVAL {int(SCAN_LOOKBACK_H)} HOUR "
        "AND value_num IS NOT NULL "
        "GROUP BY t ORDER BY t"
    )
    rows = await _watcher_ch_query(client, sql)
    if not rows:
        return np.array([]), []
    values     = np.array([float(r["v"]) for r in rows], dtype=float)
    timestamps = [float(r["t"]) / 1000.0 for r in rows]
    return values, timestamps


async def _watcher_scan_once() -> None:
    """One full pass: list active sensors, analyse, publish alarms."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        sensors = await _watcher_list_sensors(client)
        if not sensors:
            log.warning("Watcher: no active sensors in sensor_raw")
            return
        log.warning(f"Watcher: scanning {len(sensors)} sensors")
        ok = err = skip = 0
        for s in sensors:
            try:
                values, timestamps = await _watcher_fetch_history(client, s["spb_topic"])
                if len(values) < 4:
                    skip += 1
                    continue
                health = _health_index(values)
                rul    = _rul(values, timestamps)
                rec    = _recommend(health, rul, s["sensor_topic"])
                await _maybe_publish_alarm(s["sensor_topic"], health, rul, rec)
                _analyses_total.labels(status="ok").inc()
                ok += 1
            except Exception as exc:
                log.warning(f"Watcher error for {s['cell']}/{s['tag']}: {exc}")
                _analyses_total.labels(status="error").inc()
                err += 1
        log.warning(f"Watcher done: ok={ok} skip={skip} err={err}")


async def _watcher_main() -> None:
    """Watcher event-loop entry point (runs in daemon thread)."""
    _ensure_mqtt_publisher()
    await asyncio.sleep(45)  # let FastMCP server settle first
    while True:
        try:
            await _watcher_scan_once()
        except Exception as exc:
            log.warning(f"Watcher loop error: {exc}")
        await asyncio.sleep(SCAN_INTERVAL_S)


def _start_watcher() -> None:
    """Launch watcher in a daemon thread with its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_watcher_main())


if __name__ == "__main__":
    if HAS_PROMETHEUS:
        _prom_start(METRICS_PORT)
        log.warning(f"Prometheus metrics on :{METRICS_PORT}")
    watcher = threading.Thread(target=_start_watcher, name="predmaint-watcher", daemon=True)
    watcher.start()
    log.warning(f"Watcher thread started (interval={SCAN_INTERVAL_S}s, lookback={SCAN_LOOKBACK_H}h)")
    mcp.run(transport="streamable-http", host="0.0.0.0", port=5100)
