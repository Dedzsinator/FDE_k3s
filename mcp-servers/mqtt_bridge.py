#!/usr/bin/env python3
"""
MQTT → ClickHouse bridge.
Subscribes to spBv1.0/# (Sparkplug B) on NATS MQTT port,
decodes protobuf, and writes per-metric rows to mqtt_raw.

Topic mapping:
  spBv1.0/{group}/DDATA/{node}/{device} → spb/{group}/{node}/{device}/{metric}
"""
import os, sys, time, logging, threading, json
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
log = logging.getLogger("mqtt-bridge")

MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "31883"))
CH_URL    = os.getenv("CLICKHOUSE_URL", "http://127.0.0.1:32123")
CH_USER   = os.getenv("CLICKHOUSE_USER", "default")
CH_PASS   = os.getenv("CLICKHOUSE_PASSWORD", "fde-clickhouse-secret")

sys.path.insert(0, "/site-packages")
sys.path.insert(0, "/app")   # for sparkplug_b_pb2.py

# ── Dependencies ──────────────────────────────────────────────────────────────
def _pip(*pkgs):
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                    "--target=/site-packages"] + list(pkgs), check=True)

try:
    import paho.mqtt.client as mqtt
except ImportError:
    _pip("paho-mqtt"); import paho.mqtt.client as mqtt

try:
    import google.protobuf
except ImportError:
    _pip("protobuf>=4.0"); import google.protobuf

try:
    import os as _os; _os.environ.setdefault('PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION', 'python')
    import sparkplug_b_pb2 as _spb
    _HAS_SPB = True
    log.info("Sparkplug B protobuf loaded OK")
except ImportError as e:
    _HAS_SPB = False
    log.warning("sparkplug_b_pb2 not available: %s — SpB messages will be skipped", e)

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
        f"{CH_URL}/?query=INSERT+INTO+uns.mqtt_raw"
        f"+(ts,topic,value_num,value_str,status,units)+FORMAT+JSONEachRow"
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

def _row(ts_ms: int, topic: str, val_num, val_str: str = "") -> dict:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    ts_s = dt.strftime("%Y-%m-%d %H:%M:%S") + f".{ts_ms % 1000:03d}"
    return {"ts": ts_s, "topic": topic,
            "value_num": val_num, "value_str": val_str,
            "status": "", "units": ""}

# ── Sparkplug B decoder ───────────────────────────────────────────────────────
# DataType constants → value field names on the protobuf Metric object
_DT_FIELD = {
    1: "int_value", 2: "int_value", 3: "int_value", 4: "long_value",
    5: "int_value", 6: "int_value", 7: "long_value", 8: "long_value",
    9: "float_value", 10: "double_value", 11: "boolean_value",
    12: "string_value", 13: "bytes_value",
}


def _decode_spb(topic: str, payload: bytes, ts_ms: int) -> list[dict]:
    """Decode a Sparkplug B protobuf payload → list of mqtt_raw rows."""
    if not _HAS_SPB:
        return []
    # topic: spBv1.0/{group}/DDATA/{node}/{device}
    parts = topic.split("/")
    if len(parts) < 5 or parts[2] not in ("DDATA", "DBIRTH"):
        return []  # skip NBIRTH/NDEATH/CMD
    group, node, device = parts[1], parts[3], parts[4]
    prefix = f"spb/{group}/{node}/{device}"

    try:
        pb = _spb.Payload()
        pb.ParseFromString(payload)
    except Exception as e:
        log.debug("SpB parse error on %s: %s", topic, e)
        return []

    rows = []
    for m in pb.metrics:
        name = m.name
        if not name:
            continue
        dt = m.datatype
        field = _DT_FIELD.get(dt)
        val_num = None
        val_str = ""
        if field:
            raw_val = getattr(m, field, None)
            if dt == 11:  # boolean
                val_num = 1.0 if raw_val else 0.0
            elif dt == 12:  # string
                val_str = str(raw_val)[:200]
            else:
                try:
                    val_num = float(raw_val)
                except (TypeError, ValueError):
                    val_str = str(raw_val)[:200]

        # use metric-level timestamp if available, else message-level
        m_ts = m.timestamp if m.timestamp else (pb.timestamp if pb.timestamp else ts_ms)
        rows.append(_row(m_ts, f"{prefix}/{name}", val_num, val_str))

    return rows


# ── MQTT callbacks ────────────────────────────────────────────────────────────
def on_connect(client, userdata, flags, rc, properties=None):
    log.info("MQTT connected rc=%s", rc)
    client.subscribe([("spBv1.0/#", 0), ("dcsim/#", 0)])

def on_message(client, userdata, msg):
    topic = msg.topic
    if topic.startswith("$"):
        return
    ts_ms = int(time.time() * 1000)

    if topic.startswith("spBv1.0/"):
        rows = _decode_spb(topic, msg.payload, ts_ms)
        if rows:
            with _lock:
                _buf.extend(rows)
        return

    # Non-SpB: try plain float or JSON
    val_num = None
    val_str = ""
    try:
        val_num = float(msg.payload.decode("utf-8", errors="replace").strip())
    except (ValueError, UnicodeDecodeError):
        try:
            d = json.loads(msg.payload)
            if isinstance(d, (int, float)):
                val_num = float(d)
            elif isinstance(d, dict):
                with _lock:
                    for k, v in d.items():
                        try: nv = float(v)
                        except (TypeError, ValueError): nv = None
                        sv = "" if nv is not None else str(v)[:200]
                        _buf.append(_row(ts_ms, f"{topic}/{k}", nv, sv))
                return
        except Exception:
            val_str = msg.payload.decode("utf-8", errors="replace")[:200]

    with _lock:
        _buf.append(_row(ts_ms, topic, val_num, val_str))


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("MQTT→CH bridge starting  %s:%s → %s", MQTT_HOST, MQTT_PORT, CH_URL)
    threading.Thread(target=_flush_loop, daemon=True).start()
    while True:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                        client_id="fde-mqtt-ch-bridge", clean_session=True)
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
