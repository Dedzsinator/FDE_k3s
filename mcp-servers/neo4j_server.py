"""FDE Neo4j factory-graph MCP server — replaces Apache AGE backend."""
import json, logging, os, sys
os.environ["FASTMCP_DISABLE_BANNER"] = "1"
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

from fastmcp import FastMCP
from neo4j import GraphDatabase, exceptions as neo4j_exc

mcp = FastMCP("fde-factory-graph")

NEO4J_URI  = os.environ.get("NEO4J_URI",      "bolt://127.0.0.1:32687")
NEO4J_USER = os.environ.get("NEO4J_USER",     "fde_mcp_graph")
NEO4J_PASS = os.environ.get("NEO4J_PASSWORD", "fde-mcp-graph-secret")
NEO4J_DB   = os.environ.get("NEO4J_DATABASE", "neo4j")

def _driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))

def _run(cypher: str, params: dict = None) -> str:
    try:
        with _driver() as drv:
            with drv.session(database=NEO4J_DB) as ses:
                result = ses.run(cypher, params or {})
                records = [dict(r) for r in result]
                return json.dumps(records, indent=2, default=str)
    except neo4j_exc.AuthError as e:
        return json.dumps({"error": f"Auth failed: {e}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool
def cypher_query(cypher: str) -> str:
    """Execute a read-only Cypher query against the factory graph in Neo4j.

    Examples:
      MATCH (n:Equipment) RETURN n.name, n.type LIMIT 10
      MATCH (e:Equipment)-[:HAS_SENSOR]->(s:Sensor) RETURN e.name, s.name, s.unit
      MATCH p=(a:Equipment)-[:FEEDS_INTO*]->(b) RETURN [n IN nodes(p)|n.name] AS chain
    """
    return _run(cypher)


@mcp.tool
def describe_schema() -> str:
    """Return all node labels and relationship types in the factory graph."""
    labels = _run("CALL db.labels() YIELD label RETURN collect(label) AS labels")
    rels   = _run("CALL db.relationshipTypes() YIELD relationshipType RETURN collect(relationshipType) AS types")
    return json.dumps({
        "node_labels": json.loads(labels)[0].get("labels", []) if json.loads(labels) else [],
        "relationship_types": json.loads(rels)[0].get("types", []) if json.loads(rels) else [],
        "backend": "Neo4j 5 Community Edition",
        "bolt_uri": NEO4J_URI,
    }, indent=2)


@mcp.tool
def get_equipment_sensors(equipment_name: str) -> str:
    """Return all sensors attached to the named Equipment node.

    Args:
        equipment_name: e.g. 'Conveyor', 'AOI', 'ReflowOven'
    """
    return _run(
        "MATCH (e:Equipment {name: $name})-[:HAS_SENSOR]->(s:Sensor) "
        "RETURN s.name AS sensor, s.unit AS unit, s.mean AS mean_value, "
        "       s.min AS min_value, s.max AS max_value, s.anomaly AS anomaly "
        "ORDER BY s.name",
        {"name": equipment_name}
    )


@mcp.tool
def get_process_chain() -> str:
    """Return the full FEEDS_INTO process chain showing equipment order in the production line."""
    return _run(
        "MATCH (a:Equipment)-[:FEEDS_INTO]->(b:Equipment) "
        "RETURN a.name AS from_equipment, b.name AS to_equipment "
        "ORDER BY a.name"
    )


@mcp.tool
def get_line_overview() -> str:
    """Return a full overview: Line → Cell → Equipment → Sensor hierarchy."""
    return _run(
        "MATCH (l:Line)<-[:BELONGS_TO_CELL]-(c:Cell)<-[:BELONGS_TO_CELL]-(e:Equipment) "
        "OPTIONAL MATCH (e)-[:HAS_SENSOR]->(s:Sensor) "
        "RETURN l.name AS line, c.name AS cell, e.name AS equipment, "
        "       collect({name: s.name, unit: s.unit}) AS sensors "
        "ORDER BY e.name"
    )


@mcp.tool
def find_sensor(sensor_name: str) -> str:
    """Find a sensor by name and return its properties and parent equipment.

    Args:
        sensor_name: e.g. 'motor_temp', 'rpm', 'solder_quality_score'
    """
    return _run(
        "MATCH (e:Equipment)-[:HAS_SENSOR]->(s:Sensor) "
        "WHERE s.name = $name "
        "RETURN e.name AS equipment, s.name AS sensor, s.unit AS unit, "
        "       s.min AS min, s.max AS max, s.mean AS mean, s.std AS std, "
        "       s.samples AS samples, s.anomaly AS anomaly",
        {"name": sensor_name}
    )




# ── Obsidian vault config for graphify ──────────────────────────────────────

# ── Obsidian vault graphify ──────────────────────────────────────────────────
_OBS_URL = os.environ.get("OBSIDIAN_URL",     "http://192.168.100.100:27123")
_OBS_KEY = os.environ.get("OBSIDIAN_API_KEY", "a9148b82dd5e61801a167c230613c3d468da1ba27a922180f31ff5416c98c5ef")


@mcp.tool
def graphify_vault() -> str:
    """Read all Obsidian notes via REST API and build a Note knowledge graph in Neo4j.

    Creates Note nodes (path, title, tags, folder) and LINKS_TO relationships
    from [[wikilinks]]. Idempotent — safe to run repeatedly.
    """
    import urllib.request as _ur
    import urllib.parse as _up
    import re as _re
    import json as _json

    def _obs_get(path):
        req = _ur.Request(
            _OBS_URL + path,
            headers={"Authorization": "Bearer " + _OBS_KEY}
        )
        with _ur.urlopen(req, timeout=30) as r:
            return _json.loads(r.read())

    def _obs_read(vpath):
        req = _ur.Request(
            _OBS_URL + "/vault/" + _up.quote(vpath.lstrip("/")),
            headers={"Authorization": "Bearer " + _OBS_KEY}
        )
        with _ur.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")

    def _list_notes():
        result = []
        def _recurse(data, base=""):
            for entry in data.get("files", []):
                if entry.endswith("/"):
                    sub = _obs_get("/vault/" + _up.quote((base + entry).strip("/")) + "/")
                    _recurse(sub, base + entry)
                elif entry.endswith(".md"):
                    result.append((base + entry).lstrip("/"))
        _recurse(_obs_get("/vault/"))
        return result

    notes = _list_notes()
    upserted = 0

    with _driver() as drv:
        with drv.session(database=NEO4J_DB) as ses:
            ses.run(
                "CREATE CONSTRAINT note_path IF NOT EXISTS "
                "FOR (n:Note) REQUIRE n.path IS UNIQUE"
            )
            for note_path in notes:
                try:
                    raw = _obs_read(note_path)
                except Exception:
                    continue

                title = note_path.split("/")[-1].replace(".md", "")
                tags = []
                if raw.startswith("---"):
                    end = raw.find("\n---", 3)
                    if end != -1:
                        try:
                            import yaml
                            fm = yaml.safe_load(raw[3:end]) or {}
                            title = str(fm.get("title", title))
                            t = fm.get("tags", [])
                            tags = t if isinstance(t, list) else [t]
                        except Exception:
                            pass

                folder = "/".join(note_path.split("/")[:-1]) or "/"
                wikilinks = list(set(_re.findall(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]", raw)))

                ses.run(
                    "MERGE (n:Note {path: $path}) "
                    "SET n.title=$title, n.tags=$tags, n.folder=$folder, n.wikilinks=$wikilinks",
                    {"path": note_path, "title": title,
                     "tags": [str(t) for t in tags],
                     "folder": folder, "wikilinks": wikilinks}
                )
                upserted += 1

            # Build LINKS_TO edges from wikilinks
            ses.run(
                "MATCH (a:Note) "
                "FOREACH (link IN a.wikilinks | "
                "  MERGE (b:Note {path: link + '.md'}) "
                "  MERGE (a)-[:LINKS_TO]->(b))"
            )

    return _json.dumps({"status": "ok", "notes_graphified": upserted, "total": len(notes)})


@mcp.tool
def vault_graph_stats() -> str:
    """Return counts of Note nodes and LINKS_TO relationships in the knowledge graph."""
    n = json.loads(_run("MATCH (n:Note) RETURN count(n) AS cnt"))
    lk = json.loads(_run("MATCH ()-[r:LINKS_TO]->() RETURN count(r) AS cnt"))
    return json.dumps({
        "notes": n[0]["cnt"] if n else 0,
        "links": lk[0]["cnt"] if lk else 0,
    })


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=9106)
