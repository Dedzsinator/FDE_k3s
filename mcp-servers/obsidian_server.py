"""
FDE Obsidian MCP server — Gyberrange Demo (192.168.100.100).

Connects to the Obsidian Local REST API on the gyberrange-demo host
via its LAN relay (http://192.168.100.100:27123).
Transport: streamable-http on 0.0.0.0:9108 (SSH-tunneled from Windows).
"""

from __future__ import annotations

import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

os.environ["FASTMCP_DISABLE_BANNER"] = "1"
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

from fastmcp import FastMCP

mcp = FastMCP("fde-obsidian")

# ── Config ──────────────────────────────────────────────────────────────────
_BASE    = os.environ.get("OBSIDIAN_URL",     "http://192.168.100.100:27123")
_API_KEY = os.environ.get("OBSIDIAN_API_KEY", "c4454fff5ac7e8282d87ecb89746734a76b1051dab1a1ce2a93ce43e08313a39")
_MAX_BYTES = 256 * 1024
_TIMEOUT   = 30


def _req(method: str, path: str, body: bytes | None = None) -> str:
    headers: dict[str, str] = {"Authorization": f"Bearer {_API_KEY}"}
    if body is not None:
        headers["Content-Type"] = "text/markdown"
    req = urllib.request.Request(f"{_BASE}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        return f"[HTTP {e.code} {e.reason}]\n{e.read().decode('utf-8', errors='replace')}"
    except urllib.error.URLError as e:
        return f"[connection error] {e.reason} — is Obsidian running with the Local REST API plugin active?"
    except TimeoutError:
        return f"[timeout] no response within {_TIMEOUT}s"
    if len(data) > _MAX_BYTES:
        data = data[:_MAX_BYTES] + b"\n[... truncated ...]"
    return data.decode("utf-8", errors="replace")


def _vp(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


@mcp.tool
def list_vault(path: str = "/") -> str:
    """List files and folders inside the Obsidian vault.
    path is vault-relative; use '/' for the root.
    Returns a JSON array of file/folder entries."""
    p = _vp(path)
    if not p.endswith("/"):
        p += "/"
    return _req("GET", f"/vault{p}")


@mcp.tool
def read_note(path: str) -> str:
    """Read the Markdown content of a note.
    path is vault-relative, e.g. 'folder/note.md'."""
    return _req("GET", f"/vault{_vp(path)}")


@mcp.tool
def search_vault(query: str) -> str:
    """Full-text search across all notes in the vault.
    Returns matching files with context snippets."""
    return _req("POST", f"/search/simple/?query={urllib.parse.quote(query)}")


@mcp.tool
def write_note(path: str, content: str) -> str:
    """Create or fully overwrite a note.
    path is vault-relative, e.g. 'folder/note.md'.
    content is the complete Markdown to write."""
    return _req("PUT", f"/vault{_vp(path)}", body=content.encode())


@mcp.tool
def append_note(path: str, content: str) -> str:
    """Append Markdown content to a note. Creates it if it doesn't exist.
    path is vault-relative, e.g. 'folder/note.md'."""
    return _req("POST", f"/vault{_vp(path)}", body=content.encode())


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=9108)
