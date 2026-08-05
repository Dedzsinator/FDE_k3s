"""FDE ClickHouse MCP server — K8s cluster (essen-fde)."""
import json, logging, os, sys, urllib.error, urllib.parse, urllib.request
os.environ["FASTMCP_DISABLE_BANNER"] = "1"
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
from fastmcp import FastMCP
mcp = FastMCP("fde-clickhouse")
CH_HOST = os.environ.get("CLICKHOUSE_HOST", "127.0.0.1")
CH_PORT = os.environ.get("CLICKHOUSE_PORT", "32123")
CH_USER = os.environ.get("CLICKHOUSE_USER", "default")
CH_PASS = os.environ.get("CLICKHOUSE_PASSWORD", "fde-clickhouse-secret")
CH_DB   = os.environ.get("CLICKHOUSE_DB", "default")
MAX_BYTES = 256 * 1024
def _q(sql: str) -> str:
    params = {"query": sql, "user": CH_USER, "password": CH_PASS,
              "database": CH_DB, "default_format": "JSONCompact"}
    url = f"http://{CH_HOST}:{CH_PORT}/?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=30) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        return f"[HTTP {e.code}] {e.read().decode()}"
    except urllib.error.URLError as e:
        return f"[connection error] {e.reason}"
    if len(data) > MAX_BYTES:
        data = data[:MAX_BYTES] + b"\n[... truncated ...]"
    try:
        return json.dumps(json.loads(data), indent=2)
    except Exception:
        return data.decode("utf-8", errors="replace")
@mcp.tool
def query(sql: str) -> str:
    """Execute a SQL query against ClickHouse. Returns JSONCompact results."""
    return _q(sql)
@mcp.tool
def list_databases() -> str:
    """List all databases in ClickHouse."""
    return _q("SHOW DATABASES")
@mcp.tool
def list_tables(database: str = "") -> str:
    """List tables. If database is empty lists all non-system tables."""
    if database:
        return _q(f"SHOW TABLES FROM `{database}`")
    return _q("SELECT database, name, engine FROM system.tables WHERE database NOT IN ('system','information_schema','INFORMATION_SCHEMA') ORDER BY database, name")
@mcp.tool
def describe_table(table: str, database: str = "") -> str:
    """Describe schema of a table (columns and types)."""
    ref = f"`{database}`.`{table}`" if database else f"`{table}`"
    return _q(f"DESCRIBE TABLE {ref}")
@mcp.tool
def sample_data(table: str, database: str = "", limit: int = 20) -> str:
    """Return sample rows from a table."""
    ref = f"`{database}`.`{table}`" if database else f"`{table}`"
    return _q(f"SELECT * FROM {ref} LIMIT {limit}")
if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=9105)
