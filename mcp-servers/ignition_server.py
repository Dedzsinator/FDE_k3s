"""
FDE Ignition MCP proxy — K8s cluster (essen-fde).

Proxies to the Ignition native MCP module via K8s NodePort with API token auth.
Passes transport directly to create_proxy so tool list hot-reloads each session.
"""
import logging
import os
import sys

os.environ["FASTMCP_DISABLE_BANNER"] = "1"
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy

IGNITION_URL   = os.environ.get("IGNITION_URL",   "http://127.0.0.1:30088/data/mcp/fde")
IGNITION_TOKEN = os.environ.get("IGNITION_TOKEN", "MCP:Nc8_QIEZDNJcLFLbLzHCepeZWpuRNlZTCfd1XaYLWwE")

transport = StreamableHttpTransport(
    url=IGNITION_URL,
    headers={"X-Ignition-API-Token": IGNITION_TOKEN},
)

mcp = create_proxy(transport, name="fde-ignition")

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=9104)
