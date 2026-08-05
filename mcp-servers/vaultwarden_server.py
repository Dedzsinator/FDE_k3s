"""
FDE Vaultwarden MCP server.

Authenticates against a self-hosted Vaultwarden instance, decrypts vault
items client-side (AES-256-CBC + HMAC-SHA256, Bitwarden-compatible), and
exposes search/get tools to the LLM.

Env vars:
  VW_URL       Vaultwarden base URL  (default: http://vaultwarden.uns.svc.cluster.local)
  VW_EMAIL     Account e-mail
  VW_PASSWORD  Master password
"""
import base64
import hashlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

os.environ["FASTMCP_DISABLE_BANNER"] = "1"
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

from fastmcp import FastMCP

mcp = FastMCP("fde-vaultwarden")

# ── Config ───────────────────────────────────────────────────────────────────
VW_URL      = os.environ.get("VW_URL",      "http://vaultwarden.uns.svc.cluster.local").rstrip("/")
VW_EMAIL    = os.environ.get("VW_EMAIL",    "")
VW_PASSWORD = os.environ.get("VW_PASSWORD", "")

# ── Crypto helpers (Bitwarden-compatible) ────────────────────────────────────
def _pbkdf2(password: bytes, salt: bytes, iterations: int, length: int = 32) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password, salt, iterations, dklen=length)


def _hkdf_expand(prk: bytes, info: bytes, length: int = 32) -> bytes:
    """Single-block HKDF-Expand (RFC 5869, SHA-256)."""
    import hmac as _hmac
    t = _hmac.new(prk, info + b"\x01", "sha256").digest()
    return t[:length]


def _derive_keys(password: str, email: str, kdf_iterations: int) -> tuple[bytes, bytes]:
    """Returns (enc_key, mac_key) after stretching the master key."""
    master_key = _pbkdf2(password.encode(), email.lower().encode(), kdf_iterations)
    enc_key = _hkdf_expand(master_key, b"enc")
    mac_key = _hkdf_expand(master_key, b"mac")
    return enc_key, mac_key


def _master_hash(password: str, email: str, kdf_iterations: int) -> str:
    """Returns the base64 master password hash sent to the server."""
    master_key = _pbkdf2(password.encode(), email.lower().encode(), kdf_iterations)
    pw_hash    = _pbkdf2(master_key, password.encode(), 1)
    return base64.b64encode(pw_hash).decode()


def _decrypt_sym_key(enc_sym_key: str, enc_key: bytes, mac_key: bytes) -> tuple[bytes, bytes]:
    """Decrypt the account's encKey; returns (item_enc_key, item_mac_key)."""
    raw = _aes_cbc_decrypt(enc_sym_key, enc_key, mac_key)
    return raw[:32], raw[32:]


def _aes_cbc_decrypt(cipher_string: str, enc_key: bytes, mac_key: bytes) -> bytes:
    """Decrypt a Bitwarden CipherString (type 2: AES-CBC-256 + HMAC-SHA256)."""
    import hmac as _hmac
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    # Format: "2.<iv_b64>|<ct_b64>|<mac_b64>"
    if not cipher_string:
        return b""
    head, *rest = cipher_string.split("|")
    cs_type = int(head.split(".")[0])
    iv_b64  = head.split(".")[1]

    if cs_type != 2 or len(rest) < 2:
        raise ValueError(f"Unsupported CipherString type {cs_type}")

    iv  = base64.b64decode(iv_b64)
    ct  = base64.b64decode(rest[0])
    mac = base64.b64decode(rest[1])

    # Verify MAC
    expected_mac = _hmac.new(mac_key, iv + ct, "sha256").digest()
    if not _hmac.compare_digest(expected_mac, mac):
        raise ValueError("MAC verification failed — wrong key?")

    # AES-256-CBC decrypt
    cipher    = Cipher(algorithms.AES(enc_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    plaintext = decryptor.update(ct) + decryptor.finalize()

    # Remove PKCS7 padding
    pad = plaintext[-1]
    return plaintext[:-pad]


def _decrypt_str(cipher_string: str | None, enc_key: bytes, mac_key: bytes) -> str:
    if not cipher_string:
        return ""
    try:
        return _aes_cbc_decrypt(cipher_string, enc_key, mac_key).decode("utf-8", errors="replace")
    except Exception:
        return "<decrypt-error>"


# ── Session cache ────────────────────────────────────────────────────────────
_cache: dict = {}   # keys: token, enc_key, mac_key, ciphers, expires_at


def _http(method: str, path: str, data=None, token: str | None = None,
          content_type: str = "application/json") -> dict:
    url = VW_URL + path
    body = (urllib.parse.urlencode(data).encode() if content_type == "application/x-www-form-urlencoded"
            else (json.dumps(data).encode() if data else None))
    headers = {"Content-Type": content_type, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:300]}")


def _ensure_session() -> tuple[bytes, bytes, list]:
    """Return (enc_key, mac_key, ciphers), refreshing if expired."""
    if not VW_EMAIL or not VW_PASSWORD:
        raise RuntimeError("VW_EMAIL and VW_PASSWORD env vars must be set")

    now = time.time()
    if _cache.get("expires_at", 0) > now:
        return _cache["enc_key"], _cache["mac_key"], _cache["ciphers"]

    # 1. Pre-login to get KDF params
    pre = _http("POST", "/identity/accounts/prelogin",
                {"email": VW_EMAIL}, content_type="application/json")
    kdf_iter = pre.get("kdfIterations", 600000)

    # 2. Login
    pw_hash = _master_hash(VW_PASSWORD, VW_EMAIL, kdf_iter)
    device_id = str(uuid.uuid4())
    token_resp = _http("POST", "/identity/connect/token", {
        "grant_type":       "password",
        "username":         VW_EMAIL,
        "password":         pw_hash,
        "scope":            "api offline_access",
        "client_id":        "web",
        "deviceType":       "9",
        "deviceIdentifier": device_id,
        "deviceName":       "fde-mcp",
    }, content_type="application/x-www-form-urlencoded")

    access_token = token_resp["access_token"]
    expires_in   = token_resp.get("expires_in", 3600)

    # 3. Sync
    sync = _http("GET", "/api/sync?excludeDomains=true", token=access_token)
    enc_sym_key = sync["profile"]["key"]

    # 4. Derive item keys
    enc_key, mac_key = _derive_keys(VW_PASSWORD, VW_EMAIL, kdf_iter)
    item_enc, item_mac = _decrypt_sym_key(enc_sym_key, enc_key, mac_key)

    _cache.update({
        "enc_key":    item_enc,
        "mac_key":    item_mac,
        "ciphers":    sync.get("ciphers", []),
        "expires_at": now + expires_in - 60,
    })
    return item_enc, item_mac, _cache["ciphers"]


def _decrypt_cipher(c: dict, enc_key: bytes, mac_key: bytes) -> dict:
    """Decrypt a raw cipher object into a readable dict."""
    dec = lambda s: _decrypt_str(s, enc_key, mac_key)
    name  = dec(c.get("name"))
    notes = dec(c.get("notes"))
    result: dict = {
        "id":     c.get("id"),
        "type":   c.get("type"),   # 1=login, 2=note, 3=card, 4=identity
        "name":   name,
        "notes":  notes,
        "folder": c.get("folderId"),
    }
    login = c.get("login") or {}
    if login:
        result["username"] = dec(login.get("username"))
        result["password"] = dec(login.get("password"))
        uris = [dec(u.get("uri")) for u in (login.get("uris") or [])]
        result["uris"] = [u for u in uris if u]
    return result


# ── MCP Tools ────────────────────────────────────────────────────────────────

@mcp.tool
def vault_list() -> str:
    """List all vault item names and their type (login/note/card/identity).

    Does NOT return passwords — use vault_get() for credentials.
    """
    enc_key, mac_key, ciphers = _ensure_session()
    TYPE = {1: "login", 2: "note", 3: "card", 4: "identity"}
    items = []
    for c in ciphers:
        name = _decrypt_str(c.get("name"), enc_key, mac_key)
        items.append({"name": name, "type": TYPE.get(c.get("type"), "unknown"), "id": c.get("id")})
    items.sort(key=lambda x: x["name"].lower())
    return json.dumps({"count": len(items), "items": items})


@mcp.tool
def vault_get(name: str) -> str:
    """Get credentials for a vault item by exact or partial name match.

    Returns username, password, URIs, and notes.
    Use this to retrieve service credentials, API keys, tokens, etc.

    Args:
        name: Item name to look up (case-insensitive, partial match ok)
    """
    enc_key, mac_key, ciphers = _ensure_session()
    name_lower = name.lower()
    matches = []
    for c in ciphers:
        item_name = _decrypt_str(c.get("name"), enc_key, mac_key)
        if name_lower in item_name.lower():
            matches.append(_decrypt_cipher(c, enc_key, mac_key))

    if not matches:
        return json.dumps({"error": f"No vault item matching '{name}'"})
    return json.dumps({"matches": matches})


@mcp.tool
def vault_search(query: str) -> str:
    """Search vault items by name, username, or URI.

    Returns item names and metadata (no passwords). Use vault_get() to retrieve
    credentials for a specific match.

    Args:
        query: Search string matched against name, username, and URIs
    """
    enc_key, mac_key, ciphers = _ensure_session()
    q = query.lower()
    TYPE = {1: "login", 2: "note", 3: "card", 4: "identity"}
    results = []
    for c in ciphers:
        dec = _decrypt_cipher(c, enc_key, mac_key)
        haystack = " ".join(filter(None, [
            dec.get("name", ""),
            dec.get("username", ""),
            dec.get("notes", ""),
            " ".join(dec.get("uris", [])),
        ])).lower()
        if q in haystack:
            results.append({
                "name":     dec["name"],
                "type":     TYPE.get(c.get("type"), "unknown"),
                "username": dec.get("username", ""),
                "uris":     dec.get("uris", []),
            })
    results.sort(key=lambda x: x["name"].lower())
    return json.dumps({"count": len(results), "results": results})


@mcp.tool
def vault_status() -> str:
    """Check Vaultwarden connection status and item count without decrypting."""
    if not VW_EMAIL or not VW_PASSWORD:
        return json.dumps({"status": "unconfigured", "error": "VW_EMAIL/VW_PASSWORD not set"})
    try:
        enc_key, mac_key, ciphers = _ensure_session()
        return json.dumps({"status": "ok", "url": VW_URL, "item_count": len(ciphers)})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=9111)
