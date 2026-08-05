#!/usr/bin/env python3
"""OpenAI-compatible /v1/embeddings API using fastembed (nomic-embed-text-v1.5)."""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5"
PORT = 8001

print(f"Loading {MODEL_NAME}...", flush=True)
sys.path.insert(0, "/site-packages")
from fastembed import TextEmbedding
model = TextEmbedding(model_name=MODEL_NAME)
print(f"Model ready on :{PORT}", flush=True)


class Handler(BaseHTTPRequestHandler):
    timeout = 10  # Don't block forever on TCP-only probes or stale connections

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path.rstrip("/") == "/v1/models":
            body = json.dumps({
                "object": "list",
                "data": [{"id": MODEL_NAME, "object": "model"}]
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/embeddings":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length))
        texts = req.get("input", [])
        if isinstance(texts, str):
            texts = [texts]
        vecs = list(model.embed(texts))
        data = [{"object": "embedding", "index": i, "embedding": v.tolist()}
                for i, v in enumerate(vecs)]
        body = json.dumps({
            "object": "list",
            "data": data,
            "model": MODEL_NAME,
            "usage": {"prompt_tokens": 0, "total_tokens": 0}
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print(f"Serving on 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
