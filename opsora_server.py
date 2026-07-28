#!/usr/bin/env python3
"""Opsora Agent API — OpenAI-compatible proxy with smart routing,
fallback chain, and usage tracking.
Zero dependencies beyond Python stdlib."""

import json
import os
import time
import uuid
import logging
import sqlite3
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "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")
NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DASHSCOPE_KEY = os.getenv("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"

OPSORA_API_KEYS = [k.strip() for k in os.getenv("OPSORA_API_KEYS", "").split(",") if k.strip()]
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "opsora_usage.db")

# ---------------------------------------------------------------------------
# Model Configuration — Primary + Fallback Chain
# ---------------------------------------------------------------------------

MODEL_CONFIG = {
    "opsora-fast": {
        "primary": ("nvidia", "deepseek-ai/deepseek-v4-flash"),
        "fallbacks": [("nvidia", "stepfun-ai/step-3.7-flash"), ("openrouter", "deepseek/deepseek-chat-v3-0324:free")],
        "label": "DeepSeek V4 Flash",
        "speed": "fastest",
        "description": "Ultra-fast responses for real-time applications",
        "input_price": 8,
        "output_price": 15,
    },
    "opsora-brain": {
        "primary": ("nvidia", "meta/llama-3.1-70b-instruct"),
        "fallbacks": [("nvidia", "nvidia/nemotron-3-nano-30b-a3b"), ("openrouter", "meta-llama/llama-3.1-70b-instruct:free")],
        "label": "Llama 3.1 70B",
        "speed": "fast",
        "description": "Balanced quality and speed for general tasks",
        "input_price": 20,
        "output_price": 45,
    },
    "opsora-code": {
        "primary": ("nvidia", "meta/codellama-70b"),
        "fallbacks": [("nvidia", "mistralai/codestral-22b-instruct-v0.1"), ("nvidia", "meta/llama-3.1-70b-instruct")],
        "label": "CodeLlama 70B",
        "speed": "fast",
        "description": "Optimized for code generation and debugging",
        "input_price": 20,
        "output_price": 45,
    },
    "opsora-vision": {
        "primary": ("nvidia", "meta/llama-3.2-90b-vision-instruct"),
        "fallbacks": [("nvidia", "google/gemma-4-31b-it"), ("nvidia", "microsoft/phi-3-vision-128k-instruct")],
        "label": "Llama 3.2 90B Vision",
        "speed": "moderate",
        "description": "Multimodal — understands images and text together",
        "input_price": 25,
        "output_price": 55,
    },
    "opsora-reason": {
        "primary": ("nvidia", "deepseek-ai/deepseek-v4-pro"),
        "fallbacks": [("nvidia", "nvidia/cosmos-reason2-8b"), ("openrouter", "deepseek/deepseek-r1:free")],
        "label": "DeepSeek V4 Pro",
        "speed": "moderate",
        "description": "Advanced reasoning for complex problems",
        "input_price": 40,
        "output_price": 90,
    },
    "opsora-max": {
        "primary": ("nvidia", "nvidia/llama-3.1-nemotron-ultra-253b-v1"),
        "fallbacks": [("nvidia", "nvidia/nemotron-3-ultra-550b-a55b"), ("openrouter", "nvidia/llama-3.1-nemotron-ultra-253b:free")],
        "label": "Nemotron Ultra 253B",
        "speed": "slower",
        "description": "Our most powerful model for maximum quality",
        "input_price": 80,
        "output_price": 180,
    },
}

# ---------------------------------------------------------------------------
# Usage Tracking (SQLite)
# ---------------------------------------------------------------------------

_db_lock = threading.Lock()

def _init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            id TEXT PRIMARY KEY,
            timestamp REAL NOT NULL,
            api_key TEXT NOT NULL,
            model TEXT NOT NULL,
            real_model TEXT NOT NULL,
            provider TEXT NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            latency_ms REAL DEFAULT 0,
            status INTEGER DEFAULT 200,
            ip TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_usage_key ON usage(api_key)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(timestamp)
    """)
    conn.commit()
    conn.close()

def _log_usage(record):
    with _db_lock:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.get("id", str(uuid.uuid4())),
                    record.get("timestamp", time.time()),
                    record.get("api_key", ""),
                    record.get("model", ""),
                    record.get("real_model", ""),
                    record.get("provider", ""),
                    record.get("input_tokens", 0),
                    record.get("output_tokens", 0),
                    record.get("total_tokens", 0),
                    record.get("latency_ms", 0),
                    record.get("status", 200),
                    record.get("ip", ""),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logging.getLogger("opsora").error("DB write error: %s", e)

def _get_usage_stats(api_key=None, days=30):
    with _db_lock:
        try:
            conn = sqlite3.connect(DB_PATH)
            cutoff = time.time() - (days * 86400)
            if api_key:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(total_tokens),0), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) FROM usage WHERE api_key=? AND timestamp>?",
                    (api_key, cutoff),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(total_tokens),0), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) FROM usage WHERE timestamp>?",
                    (cutoff,),
                ).fetchone()
            conn.close()
            return {"requests": row[0], "total_tokens": row[1], "input_tokens": row[2], "output_tokens": row[3]}
        except Exception:
            return {"requests": 0, "total_tokens": 0, "input_tokens": 0, "output_tokens": 0}

# ---------------------------------------------------------------------------
# Provider Backends
# ---------------------------------------------------------------------------

PROVIDERS = {
    "nvidia": {"base": NVIDIA_BASE, "key": NVIDIA_API_KEY},
    "openrouter": {"base": OPENROUTER_BASE, "key": OPENROUTER_KEY},
    "dashscope": {"base": DASHSCOPE_BASE, "key": DASHSCOPE_KEY},
}

def _call_provider(provider_name, endpoint, body, timeout=120):
    """Call a provider and return (response_dict, status_code)."""
    prov = PROVIDERS.get(provider_name)
    if not prov or not prov["key"]:
        return {"error": {"message": f"Provider {provider_name} not configured.", "type": "config_error"}}, 503

    url = f"{prov['base']}{endpoint}"
    data = json.dumps(body).encode()

    req = Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {prov['key']}")
    req.add_header("Content-Type", "application/json")

    # OpenRouter-specific header
    if provider_name == "openrouter":
        req.add_header("HTTP-Referer", "https://opsora.id")
        req.add_header("X-Title", "Opsora Agent API")

    try:
        resp = urlopen(req, timeout=timeout)
        result = json.loads(resp.read().decode())
        return result, resp.status
    except HTTPError as e:
        try:
            err_body = json.loads(e.read().decode())
        except Exception:
            err_body = {"error": {"message": f"Upstream {provider_name} error: {e.code}", "type": "upstream_error"}}
        return err_body, e.code
    except (URLError, Exception) as e:
        return {"error": {"message": f"Connection error to {provider_name}: {str(e)[:100]}", "type": "connection_error"}}, 502

def _route_with_fallback(model_alias, endpoint, body):
    """Try primary provider, then fallbacks. Returns (result, status, provider_used, real_model)."""
    config = MODEL_CONFIG.get(model_alias)
    if not config:
        # Unknown model — pass through to NVIDIA directly
        return _call_provider("nvidia", endpoint, body) + ("nvidia", model_alias)

    primary_prov, primary_model = config["primary"]
    body["model"] = primary_model
    result, status = _call_provider(primary_prov, endpoint, body)
    if status == 200:
        return result, status, primary_prov, primary_model

    # Try fallbacks
    for fb_prov, fb_model in config.get("fallbacks", []):
        body["model"] = fb_model
        result, status = _call_provider(fb_prov, endpoint, body)
        if status == 200:
            return result, status, fb_prov, fb_model

    # All failed
    return result, status, primary_prov, primary_model

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("opsora")

# ---------------------------------------------------------------------------
# Request Handler
# ---------------------------------------------------------------------------

class OpsoraHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.info(fmt, *args)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def _authenticate(self):
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if OPSORA_API_KEYS and token not in OPSORA_API_KEYS:
            self._send_json({"error": {"message": "Invalid API key.", "type": "auth_error"}}, 401)
            return None
        return token

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Powered-By", "Opsora Agent API")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    # --- Routes ---

    def do_GET(self):
        if self.path == "/health":
            stats = _get_usage_stats()
            self._send_json({
                "status": "ok",
                "service": "opsora-agent-api",
                "models": list(MODEL_CONFIG.keys()),
                "total_requests": stats["requests"],
                "total_tokens": stats["total_tokens"],
            })

        elif self.path == "/v1/models":
            if self._authenticate() is None:
                return
            models = []
            for alias, cfg in MODEL_CONFIG.items():
                models.append({
                    "id": alias,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "opsora",
                    "description": cfg["description"],
                    "label": cfg["label"],
                    "speed": cfg["speed"],
                })
            self._send_json({"object": "list", "data": models})

        elif self.path.startswith("/v1/usage"):
            token = self._authenticate()
            if token is None:
                return
            stats = _get_usage_stats(api_key=token if OPSORA_API_KEYS else None)
            self._send_json(stats)

        else:
            self._send_json({"error": {"message": "Not found.", "type": "invalid_request_error"}}, 404)

    def do_POST(self):
        token = self._authenticate()
        if token is None:
            return

        body = self._read_body()

        if self.path in ("/v1/chat/completions", "/v1/completions"):
            model_alias = body.get("model", "opsora-brain")
            body.pop("user", None)

            endpoint = "/chat/completions" if "chat" in self.path else "/completions"

            logger.info("→ POST %s model=%s", self.path, model_alias)
            t0 = time.time()

            result, status, provider, real_model = _route_with_fallback(model_alias, endpoint, body)
            elapsed = time.time() - t0

            # Extract token usage
            usage = result.get("usage", {}) if isinstance(result, dict) else {}
            input_tok = usage.get("prompt_tokens", 0)
            output_tok = usage.get("completion_tokens", 0)
            total_tok = usage.get("total_tokens", input_tok + output_tok)

            logger.info("← %s → %s (%s) status=%s elapsed=%.2fs tokens=%s",
                        model_alias, real_model, provider, status, elapsed, total_tok)

            # Log usage asynchronously
            _log_usage({
                "api_key": token[:8] + "..." if len(token) > 8 else token,
                "model": model_alias,
                "real_model": real_model,
                "provider": provider,
                "input_tokens": input_tok,
                "output_tokens": output_tok,
                "total_tokens": total_tok,
                "latency_ms": elapsed * 1000,
                "status": status,
                "ip": self.client_address[0],
            })

            self._send_json(result, status)
        else:
            self._send_json({"error": {"message": "Not found.", "type": "invalid_request_error"}}, 404)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _init_db()
    server = HTTPServer(("0.0.0.0", PORT), OpsoraHandler)
    logger.info("🚀 Opsora Agent API running on port %s", PORT)
    logger.info("   Models: %s", ", ".join(MODEL_CONFIG.keys()))
    logger.info("   Providers: NVIDIA NIM + OpenRouter + DashScope (fallback)")
    logger.info("   Auth: %s", "dev mode (no keys)" if not OPSORA_API_KEYS else f"{len(OPSORA_API_KEYS)} key(s)")
    logger.info("   Usage DB: %s", DB_PATH)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.shutdown()
