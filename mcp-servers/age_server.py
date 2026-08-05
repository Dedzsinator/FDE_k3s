"""FDE Apache AGE MCP server — K8s cluster (essen-fde)."""
import json, logging, os, sys
os.environ["FASTMCP_DISABLE_BANNER"] = "1"
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
from fastmcp import FastMCP
mcp = FastMCP("fde-factory-graph")
AGE_HOST = os.environ.get("AGE_HOST", "127.0.0.1")
AGE_PORT = int(os.environ.get("AGE_PORT", "32432"))
AGE_USER = os.environ.get("AGE_USER", "age")
AGE_PASS = os.environ.get("AGE_PASSWORD", "fde-age-secret")
AGE_DB   = os.environ.get("AGE_DB", "factory_graph")
def _conn():
    import psycopg2
    return psycopg2.connect(host=AGE_HOST, port=AGE_PORT,
                            user=AGE_USER, password=AGE_PASS, dbname=AGE_DB)
def _sql(sql: str, params=None) -> str:
    try:
        with _conn() as conn:
            conn.set_session(autocommit=True)
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if cur.description:
                    cols = [d[0] for d in cur.description]
                    rows = cur.fetchall()
                    return json.dumps({"columns": cols, "rows": [list(r) for r in rows]},
                                      indent=2, default=str)
                return json.dumps({"status": "ok", "rowcount": cur.rowcount})
    except Exception as e:
        return json.dumps({"error": str(e)})
@mcp.tool
def sql_query(sql: str) -> str:
    """Execute raw SQL against the factory_graph PostgreSQL/AGE database."""
    return _sql(sql)
@mcp.tool
def list_graphs() -> str:
    """List all Apache AGE graph names in the database."""
    return _sql("SELECT name FROM ag_catalog.ag_graph ORDER BY name")
@mcp.tool
def describe_graph(graph: str) -> str:
    """List vertex and edge labels in an AGE graph."""
    return _sql(
        "SELECT l.name, l.kind FROM ag_catalog.ag_label l "
        "JOIN ag_catalog.ag_graph g ON l.graph = g.graphid "
        "WHERE g.name = %s ORDER BY l.kind, l.name", (graph,))
@mcp.tool
def cypher_query(graph: str, cypher: str) -> str:
    """Execute a Cypher query on an Apache AGE graph.
    graph: graph name from list_graphs(). cypher: e.g. 'MATCH (n) RETURN n LIMIT 10'"""
    try:
        with _conn() as conn:
            conn.set_session(autocommit=True)
            with conn.cursor() as cur:
                cur.execute("LOAD 'age'")
                cur.execute("SET search_path = ag_catalog, \"$user\", public")
                cur.execute(f"SELECT * FROM cypher('{graph}', $$ {cypher} $$) AS (result agtype)")
                rows = cur.fetchall()
                return json.dumps([str(r[0]) for r in rows], indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})
@mcp.tool
def list_tables() -> str:
    """List user tables in the factory_graph database."""
    return _sql("SELECT schemaname, tablename FROM pg_tables "
                "WHERE schemaname NOT IN ('pg_catalog','information_schema','ag_catalog') "
                "ORDER BY schemaname, tablename")
if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=9106)
