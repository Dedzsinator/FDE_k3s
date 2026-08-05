#!/usr/bin/env python3
"""
Sparkplug-B → ClickHouse decoder bridge.

EMQX Community edition cannot decode Sparkplug-B protobuf in its rule engine
(sparkplug_decode is Enterprise-only), so this lean sidecar subscribes to
spBv1.0/# on EMQX, decodes the protobuf, and writes one row per metric into
the unified uns.sensor_raw table.

Topic:  spBv1.0/{group}/DDATA/{node}/{device}
Row:    ts, topic=spBv1.0/{group}/DDATA/{node}/{device}/{metric},
        cell={device}, tag={metric}, value_num, value_str
"""
import os, sys, time, logging, threading, json
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
log = logging.getLogger("spb-bridge")

MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "31883"))
CH_URL    = os.getenv("CLICKHOUSE_URL", "http://127.0.0.1:32123")
CH_USER   = os.getenv("CLICKHOUSE_USER", "default")
CH_PASS   = os.getenv("CLICKHOUSE_PASSWORD", "fde-clickhouse-secret")

sys.path.insert(0, "/site-packages")
sys.path.insert(0, "/app")   # for sparkplug_b_pb2.py

import paho.mqtt.client as mqtt
os.environ.setdefault('PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION', 'python')
import sparkplug_b_pb2 as _spb
log.info("Sparkplug B protobuf loaded OK")

import urllib.request, urllib.parse

# ── ClickHouse writer ─────────────────────────────────────────────────────────
_buf: list[dict] = []
_lock = threading.Lock()

def _flush():
    with _lock:
        if not _buf:
            return
        rows = list(_buf)
        _buf.clear()

    body = "\n".join(json.dumps(r) for r in rows).encode()
    url = (
        f"{CH_URL}/?query=INSERT+INTO+uns.sensor_raw"
        f"+(ts,topic,cell,tag,value_num,value_str)+FORMAT+JSONEachRow"
        f"&user={urllib.parse.quote(CH_USER)}&password={urllib.parse.quote(CH_PASS)}"
    )
    try:
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/octet-stream"})
        urllib.request.urlopen(req, timeout=10)
        log.info("Flushed %d rows", len(rows))
    except urllib.request.HTTPError as e:
        log.error("ClickHouse %s: %s", e.code, e.read().decode()[:200])
    except Exception as e:
        log.error("ClickHouse error: %s", e)

def _flush_loop():
    while True:
        time.sleep(5)
        try: _flush()
        except Exception as e: log.error("flush: %s", e)

def _row(ts_ms: int, topic: str, cell: str, tag: str, val_num, val_str: str = "") -> dict:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    ts_s = dt.strftime("%Y-%m-%d %H:%M:%S") + f".{ts_ms % 1000:03d}"
    return {"ts": ts_s, "topic": topic, "cell": cell, "tag": tag,
            "value_num": val_num, "value_str": val_str}

# ── Sparkplug B decoder ───────────────────────────────────────────────────────
_DT_FIELD = {
    1: "int_value", 2: "int_value", 3: "int_value", 4: "long_value",
    5: "int_value", 6: "int_value", 7: "long_value", 8: "long_value",
    9: "float_value", 10: "double_value", 11: "boolean_value",
    12: "string_value", 13: "bytes_value",
}

def _decode_spb(topic: str, payload: bytes, ts_ms: int) -> list[dict]:
    # topic: spBv1.0/{group}/DDATA/{node}/{device}
    parts = topic.split("/")
    if len(parts) < 5 or parts[2] not in ("DDATA", "DBIRTH"):
        return []  # skip NBIRTH/NDEATH/CMD/STATE
    device = parts[4]

    try:
        pb = _spb.Payload()
        pb.ParseFromString(payload)
    except Exception as e:
        log.debug("SpB parse error on %s: %s", topic, e)
        return []

    rows = []
    for m in pb.metrics:
        name = m.name
        if not name or name in ("bdSeq", "Node Control/Rebirth"):
            continue
        dt = m.datatype
        field = _DT_FIELD.get(dt)
        val_num = None
        val_str = ""
        if field:
            raw_val = getattr(m, field, None)
            if dt == 11:      # boolean
                val_num = 1.0 if raw_val else 0.0
            elif dt == 12:    # string
                val_str = str(raw_val)[:200]
            elif dt == 13:    # bytes
                val_str = ""
            else:
                try:
                    val_num = float(raw_val)
                except (TypeError, ValueError):
                    val_str = str(raw_val)[:200]

        m_ts = m.timestamp if m.timestamp else (pb.timestamp if pb.timestamp else ts_ms)
        rows.append(_row(m_ts, f"{topic}/{name}", device, name, val_num, val_str))

    return rows

# ── MQTT callbacks ────────────────────────────────────────────────────────────
def on_connect(client, userdata, flags, rc, properties=None):
    log.info("MQTT connected rc=%s", rc)
    client.subscribe([("spBv1.0/+/DDATA/#", 0), ("spBv1.0/+/DBIRTH/#", 0)])

def on_message(client, userdata, msg):
    topic = msg.topic
    if topic.startswith("$") or not topic.startswith("spBv1.0/"):
        return
    ts_ms = int(time.time() * 1000)
    rows = _decode_spb(topic, msg.payload, ts_ms)
    if rows:
        with _lock:
            _buf.extend(rows)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("SpB→CH bridge starting  %s:%s → %s (uns.sensor_raw)", MQTT_HOST, MQTT_PORT, CH_URL)
    threading.Thread(target=_flush_loop, daemon=True).start()
    while True:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                        client_id="fde-spb-ch-bridge", clean_session=True)
        c.on_connect = on_connect
        c.on_message = on_message
        try:
            c.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
            c.loop_forever()
        except Exception as e:
            log.error("MQTT error: %s — retry in 10s", e)
            time.sleep(10)

if __name__ == "__main__":
    main()
