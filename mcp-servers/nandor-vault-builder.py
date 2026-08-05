#!/usr/bin/env python3
"""
nandor-vault-builder.py
=======================

Read credentials from k8s secrets (uns namespace) on the nandor cluster
and push them as login entries into the self-hosted Vaultwarden instance.

Run directly on the nandor node (needs kubectl access + pip deps from
/home/nandor/mcp-servers/venv, or install cryptography via pip).

Usage:
    python3 nandor-vault-builder.py [--dry-run] [--replace]

Flags:
    --dry-run   Print entries that would be created; don't write to Vaultwarden
    --replace   Delete existing vault items with the same name before re-creating
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

# ── Config ────────────────────────────────────────────────────────────────────
VW_URL      = os.environ.get("VW_URL",      "http://10.102.104.148")
VW_EMAIL    = os.environ.get("VW_EMAIL",    "nandor.degi@accenture.com")
VW_PASSWORD = os.environ.get("VW_PASSWORD", "admin")
NAMESPACE   = os.environ.get("K8S_NS",      "uns")
KDF_ITER    = 600000

# ── Crypto (same as vaultwarden_server.py) ────────────────────────────────────
def _pbkdf2(pw, salt, n, l=32):
    return hashlib.pbkdf2_hmac("sha256", pw, salt, n, dklen=l)

def _hkdf_expand(prk, info, l=32):
    import hmac
    return hmac.new(prk, info + b"\x01", "sha256").digest()[:l]

def _master_hash(pw, email, n):
    mk = _pbkdf2(pw.encode(), email.lower().encode(), n)
    return base64.b64encode(_pbkdf2(mk, pw.encode(), 1)).decode()

def _aes_cbc_enc(pt, key, iv):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    pad = 16 - len(pt) % 16
    pt += bytes([pad] * pad)
    e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return e.update(pt) + e.finalize()

def _cs(plaintext: str, ek: bytes, mk: bytes) -> str:
    """Encrypt plaintext as a Bitwarden CipherString (type 2)."""
    import hmac as h
    if not plaintext:
        return ""
    pt  = plaintext.encode()
    iv  = os.urandom(16)
    ct  = _aes_cbc_enc(pt, ek, iv)
    mac = h.new(mk, iv + ct, "sha256").digest()
    return ("2." + base64.b64encode(iv).decode()
            + "|" + base64.b64encode(ct).decode()
            + "|" + base64.b64encode(mac).decode())

# ── HTTP helpers ──────────────────────────────────────────────────────────────
def _http(method, path, data=None, token=None, ct="application/json"):
    url  = VW_URL + path
    body = (urllib.parse.urlencode(data).encode()
            if ct == "application/x-www-form-urlencoded"
            else (json.dumps(data).encode() if data else None))
    hdrs = {"Content-Type": ct, "Accept": "application/json"}
    if token:
        hdrs["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

# ── Vaultwarden login + key derivation ────────────────────────────────────────
def vw_login():
    s, pre = _http("POST", "/identity/accounts/prelogin", {"email": VW_EMAIL})
    if s != 200:
        raise RuntimeError(f"prelogin failed {s}: {pre}")
    kdf_iter = pre.get("kdfIterations", KDF_ITER)

    pw_hash   = _master_hash(VW_PASSWORD, VW_EMAIL, kdf_iter)
    device_id = str(uuid.uuid4())
    s, tok = _http("POST", "/identity/connect/token", {
        "grant_type": "password", "username": VW_EMAIL, "password": pw_hash,
        "scope": "api offline_access", "client_id": "web",
        "deviceType": "9", "deviceIdentifier": device_id, "deviceName": "vault-builder",
    }, ct="application/x-www-form-urlencoded")
    if s != 200:
        raise RuntimeError(f"login failed {s}: {tok}")
    access_token = tok["access_token"]

    s, sync = _http("GET", "/api/sync?excludeDomains=true", token=access_token)
    if s != 200:
        raise RuntimeError(f"sync failed {s}: {sync}")

    mk  = _pbkdf2(VW_PASSWORD.encode(), VW_EMAIL.lower().encode(), kdf_iter)
    ek  = _hkdf_expand(mk, b"enc")
    mak = _hkdf_expand(mk, b"mac")

    # Decrypt sym key from profile
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    import hmac as h
    enc_sym = sync["profile"]["key"]
    head, *rest = enc_sym.split("|")
    iv  = base64.b64decode(head.split(".")[1])
    ct  = base64.b64decode(rest[0])
    mac = base64.b64decode(rest[1])
    exp = h.new(mak, iv + ct, "sha256").digest()
    if not h.compare_digest(exp, mac):
        raise RuntimeError("sym key MAC mismatch — wrong password?")
    dec = Cipher(algorithms.AES(ek), modes.CBC(iv)).decryptor()
    raw = dec.update(ct) + dec.finalize()
    pad = raw[-1]; raw = raw[:-pad]
    sym_ek, sym_mk = raw[:32], raw[32:]

    existing = {_dec_field(c.get("name", ""), sym_ek, sym_mk): c
                for c in sync.get("ciphers", [])}
    return access_token, sym_ek, sym_mk, existing

def _dec_field(cs: str, ek, mk) -> str:
    if not cs or not cs.startswith("2."):
        return cs or ""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    import hmac as h
    head, *rest = cs.split("|")
    iv  = base64.b64decode(head.split(".")[1])
    ct  = base64.b64decode(rest[0])
    mac = base64.b64decode(rest[1])
    exp = h.new(mk, iv + ct, "sha256").digest()
    if not h.compare_digest(exp, mac):
        return "<mac-err>"
    dec = Cipher(algorithms.AES(ek), modes.CBC(iv)).decryptor()
    raw = dec.update(ct) + dec.finalize()
    pad = raw[-1]
    return raw[:-pad].decode("utf-8", errors="replace")

# ── k8s secret reader ─────────────────────────────────────────────────────────
def k8s_secret(name):
    r = subprocess.run(
        ["kubectl", "get", "secret", "-n", NAMESPACE, name, "-o", "json"],
        capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return {}
    obj  = json.loads(r.stdout)
    data = obj.get("data") or {}
    return {k: base64.b64decode(v).decode("utf-8", errors="replace")
            for k, v in data.items()}

def k8s_deploy_env(deploy_name):
    """Extract env vars from a deployment's first container."""
    r = subprocess.run(
        ["kubectl", "get", "deploy", "-n", NAMESPACE, deploy_name, "-o", "json"],
        capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return {}
    obj = json.loads(r.stdout)
    containers = (obj.get("spec", {}).get("template", {})
                     .get("spec", {}).get("containers", []))
    env_out = {}
    for c in containers:
        for e in (c.get("env") or []):
            if e.get("value"):
                env_out[e["name"]] = e["value"]
    return env_out

# ── Vault entry builder ───────────────────────────────────────────────────────
def build_entry(title, username, password, url, notes, ek, mk):
    e = lambda s: _cs(s, ek, mk)
    return {
        "type":     1,
        "name":     e(title),
        "notes":    e(notes),
        "reprompt": 0,
        "login": {
            "username": e(username),
            "password": e(password),
            "uris":     [{"uri": e(url), "match": None}] if url else [],
        },
        "fields": [],
    }

def push_entry(entry_dict, token, existing, replace):
    title_cs = entry_dict["name"]
    # We can't easily compare encrypted names without decrypting; use existing map
    # (already decrypted names → cipher objects passed from vw_login)
    # The 'existing' dict is keyed by plaintext name.
    # We need the plaintext title from entry_dict — it's encrypted so we can't recover it here.
    # Caller passes plaintext title separately.
    s, r = _http("POST", "/api/ciphers", entry_dict, token=token)
    return s, r

# ── Service definitions ───────────────────────────────────────────────────────
def collect_entries():
    """Return list of (title, username, password, url, notes) tuples."""
    entries = []

    # ── Ignition Gateway ──────────────────────────────────────────────────────
    sec = k8s_secret("fde-ignition-ignition-auth")
    if sec:
        entries.append((
            "Ignition Gateway",
            sec.get("IGNITION_ADMIN_USERNAME", "admin"),
            sec.get("IGNITION_ADMIN_PASSWORD", ""),
            "http://192.168.100.102:8088",
            "k8s secret: fde-ignition-ignition-auth\nDesigner: tcp://192.168.100.102:8060",
        ))

    # ── Grafana ───────────────────────────────────────────────────────────────
    sec = k8s_secret("fde-monitoring-grafana")
    if sec:
        entries.append((
            "Grafana",
            sec.get("admin-user", "admin"),
            sec.get("admin-password", ""),
            "http://192.168.100.102:3000",
            "k8s secret: fde-monitoring-grafana",
        ))

    # ── Neo4j ─────────────────────────────────────────────────────────────────
    sec = k8s_secret("fde-neo4j-secret")
    if sec:
        neo4j_auth = sec.get("NEO4J_AUTH", "")
        # NEO4J_AUTH is "neo4j/<password>"
        neo4j_user = "neo4j"
        neo4j_pass = sec.get("neo4j-password", "")
        if "/" in neo4j_auth and not neo4j_pass:
            neo4j_user, neo4j_pass = neo4j_auth.split("/", 1)
        entries.append((
            "Neo4j",
            neo4j_user,
            neo4j_pass,
            "bolt://192.168.100.102:7687",
            "k8s secret: fde-neo4j-secret\nHTTP: http://192.168.100.102:7474",
        ))

    # ── pgAdmin ───────────────────────────────────────────────────────────────
    sec = k8s_secret("fde-pgadmin-pgadmin-auth")
    if sec:
        entries.append((
            "pgAdmin",
            sec.get("PGADMIN_DEFAULT_EMAIL", ""),
            sec.get("PGADMIN_DEFAULT_PASSWORD", ""),
            "http://192.168.100.102:8082",
            "k8s secret: fde-pgadmin-pgadmin-auth",
        ))

    # ── MaestroHub (n8n) ──────────────────────────────────────────────────────
    sec = k8s_secret("fde-maestrohub-maestrohub-auth")
    if sec:
        entries.append((
            "MaestroHub (n8n)",
            sec.get("N8N_BASIC_AUTH_USER", ""),
            sec.get("N8N_BASIC_AUTH_PASSWORD", ""),
            "http://192.168.100.102:5678",
            "k8s secret: fde-maestrohub-maestrohub-auth",
        ))

    # ── Vaultwarden admin token ───────────────────────────────────────────────
    sec = k8s_secret("vaultwarden-auth")
    if sec:
        entries.append((
            "Vaultwarden Admin",
            "",
            sec.get("ADMIN_TOKEN", ""),
            "http://192.168.100.102/admin",
            "k8s secret: vaultwarden-auth\nAdmin panel token (not a login password).",
        ))

    # ── EMQX ─────────────────────────────────────────────────────────────────
    emqx_env = k8s_deploy_env("emqx") if not None else {}
    emqx_user = emqx_env.get("EMQX_DASHBOARD__DEFAULT_USERNAME",
                emqx_env.get("EMQX_DEFAULT_APPLICATION__ID", "admin"))
    emqx_pass = emqx_env.get("EMQX_DASHBOARD__DEFAULT_PASSWORD",
                emqx_env.get("EMQX_DEFAULT_APPLICATION__SECRET", ""))
    entries.append((
        "EMQX",
        emqx_user or "admin",
        emqx_pass or "(check EMQX dashboard — default: public)",
        "http://192.168.100.102:18083",
        "MQTT broker dashboard\nMQTT: mqtt://192.168.100.102:1883",
    ))

    # ── Qdrant ────────────────────────────────────────────────────────────────
    qdrant_env = k8s_deploy_env("qdrant") if not None else {}
    qdrant_key = qdrant_env.get("QDRANT__SERVICE__API_KEY", "")
    entries.append((
        "Qdrant",
        "",
        qdrant_key or "(no API key — open access)",
        "http://192.168.100.102:6333",
        "Vector database REST API\nDashboard: http://192.168.100.102:6333/dashboard",
    ))

    # ── ClickHouse ────────────────────────────────────────────────────────────
    ch_env = k8s_deploy_env("clickhouse") if not None else {}
    ch_user = ch_env.get("CLICKHOUSE_USER", ch_env.get("CLICKHOUSE_DEFAULT_USER", "default"))
    ch_pass = ch_env.get("CLICKHOUSE_PASSWORD", ch_env.get("CLICKHOUSE_DEFAULT_PASSWORD", ""))
    entries.append((
        "ClickHouse",
        ch_user or "default",
        ch_pass or "(check ClickHouse config)",
        "http://192.168.100.102:8123",
        "ClickHouse HTTP interface\nNative: clickhouse://192.168.100.102:9000",
    ))

    return entries

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Print entries without writing to Vaultwarden")
    parser.add_argument("--replace", action="store_true",
                        help="Delete existing items with same name before creating")
    args = parser.parse_args()

    print("[*] Collecting credentials from k8s secrets …")
    entries = collect_entries()
    print(f"[*] Found {len(entries)} entries to import\n")

    if args.dry_run:
        for title, user, pw, url, notes in entries:
            masked = pw[:4] + "…" if len(pw) > 4 else "***"
            print(f"  {title:<30}  user={user or '—'}  pass={masked}  url={url}")
        print("\n[i] Dry run — nothing written.")
        return

    print("[*] Logging in to Vaultwarden …")
    try:
        token, ek, mk, existing = vw_login()
    except Exception as ex:
        print(f"[!] Login failed: {ex}")
        sys.exit(1)
    print(f"[+] Logged in — {len(existing)} items already in vault\n")

    created = 0
    skipped = 0
    for title, user, pw, url, notes in entries:
        if title in existing:
            if args.replace:
                cid = existing[title]["id"]
                s, _ = _http("DELETE", f"/api/ciphers/{cid}", token=token)
                print(f"  [~] Replaced  {title}")
            else:
                print(f"  [=] Skipped   {title}  (already exists — use --replace to overwrite)")
                skipped += 1
                continue

        entry = build_entry(title, user, pw, url, notes, ek, mk)
        s, r = _http("POST", "/api/ciphers", entry, token=token)
        if s == 200:
            print(f"  [+] Created   {title}")
            created += 1
        else:
            print(f"  [!] Failed    {title}: {s} {str(r)[:120]}")

    print(f"\n[+] Done — {created} created, {skipped} skipped")
    if skipped:
        print("    Run with --replace to overwrite existing items.")

if __name__ == "__main__":
    main()
