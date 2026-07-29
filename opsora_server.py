#!/usr/bin/env python3
"""Opsora Agent API — OpenAI-compatible proxy with smart routing,
fallback chain, streaming (SSE), usage tracking, and production hardening.
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

from agent_router import route_agent_request, route_agent_streaming
from billing import BillingEngine

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

# Retry config for transient errors
MAX_RETRIES = 2
RETRY_STATUS_CODES = {502, 503}
RETRY_BASE_DELAY = 0.5  # seconds, exponential: 0.5, 1.0

# Billing engine (initialized at startup)
_billing = None

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
        "primary": ("nvidia", "nvidia/llama-3.3-nemotron-super-49b-v1"),
        "fallbacks": [("nvidia", "meta/llama-3.1-70b-instruct"), ("openrouter", "meta-llama/llama-3.1-70b-instruct:free")],
        "label": "Nemotron Super 49B",
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
        "primary": ("nvidia", "nvidia/nemotron-3-ultra-550b-a55b"),
        "fallbacks": [("nvidia", "nvidia/llama-3.3-nemotron-super-49b-v1"), ("openrouter", "nvidia/llama-3.1-nemotron-ultra-253b:free")],
        "label": "Nemotron Ultra 550B",
        "speed": "slower",
        "description": "Our most powerful model for maximum quality",
        "input_price": 80,
        "output_price": 180,
    },
    "opsora-agent": {
        "primary": ("agent_router", "opsora-agent"),
        "fallbacks": [],
        "label": "Opsora Agent",
        "speed": "variable",
        "description": "AI-powered router — automatically picks the best model for your request",
        "input_price": 40,
        "output_price": 90,
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_key ON usage(api_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_model ON usage(model)")
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
    """Basic usage stats for a given time window."""
    with _db_lock:
        try:
            conn = sqlite3.connect(DB_PATH)
            cutoff = time.time() - (days * 86400)
            if api_key:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(total_tokens),0), "
                    "COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) "
                    "FROM usage WHERE api_key=? AND timestamp>?",
                    (api_key, cutoff),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(total_tokens),0), "
                    "COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) "
                    "FROM usage WHERE timestamp>?",
                    (cutoff,),
                ).fetchone()
            conn.close()
            return {
                "requests": row[0],
                "total_tokens": row[1],
                "input_tokens": row[2],
                "output_tokens": row[3],
            }
        except Exception:
            return {"requests": 0, "total_tokens": 0, "input_tokens": 0, "output_tokens": 0}


def _get_enhanced_usage_stats(api_key=None):
    """Enhanced usage stats: all-time, 24h, 1h, per-model breakdown."""
    with _db_lock:
        try:
            conn = sqlite3.connect(DB_PATH)
            now = time.time()
            cutoff_1h = now - 3600
            cutoff_24h = now - 86400

            key_filter = ""
            key_param = ()
            if api_key:
                key_filter = " AND api_key=?"
                key_param = (api_key,)

            def _stats(cutoff_ts):
                row = conn.execute(
                    f"SELECT COUNT(*), COALESCE(SUM(total_tokens),0), "
                    f"COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) "
                    f"FROM usage WHERE timestamp>?{key_filter}",
                    (cutoff_ts,) + key_param,
                ).fetchone()
                return {
                    "requests": row[0],
                    "total_tokens": row[1],
                    "input_tokens": row[2],
                    "output_tokens": row[3],
                }

            all_time = _stats(0)
            last_24h = _stats(cutoff_24h)
            last_1h = _stats(cutoff_1h)

            # Per-model: top 5 by request count (last 30 days)
            cutoff_30d = now - (30 * 86400)
            rows = conn.execute(
                f"SELECT model, COUNT(*) as cnt, "
                f"COALESCE(SUM(total_tokens),0), "
                f"COALESCE(AVG(latency_ms),0) "
                f"FROM usage WHERE timestamp>?{key_filter} "
                f"GROUP BY model ORDER BY cnt DESC LIMIT 5",
                (cutoff_30d,) + key_param,
            ).fetchall()

            per_model = []
            for r in rows:
                per_model.append({
                    "model": r[0],
                    "requests": r[1],
                    "total_tokens": r[2],
                    "avg_latency_ms": round(r[3], 1),
                })

            conn.close()
            return {
                "all_time": all_time,
                "last_24h": last_24h,
                "last_1h": last_1h,
                "top_models": per_model,
            }
        except Exception as e:
            logging.getLogger("opsora").error("Enhanced stats error: %s", e)
            return {
                "all_time": {"requests": 0, "total_tokens": 0, "input_tokens": 0, "output_tokens": 0},
                "last_24h": {"requests": 0, "total_tokens": 0, "input_tokens": 0, "output_tokens": 0},
                "last_1h": {"requests": 0, "total_tokens": 0, "input_tokens": 0, "output_tokens": 0},
                "top_models": [],
            }


# ---------------------------------------------------------------------------
# Provider Backends
# ---------------------------------------------------------------------------

PROVIDERS = {
    "nvidia": {"base": NVIDIA_BASE, "key": NVIDIA_API_KEY},
    "openrouter": {"base": OPENROUTER_BASE, "key": OPENROUTER_KEY},
    "dashscope": {"base": DASHSCOPE_BASE, "key": DASHSCOPE_KEY},
}


def _format_error(message, error_type="server_error", code=None):
    """Build an OpenAI-compatible error object."""
    err = {"message": message, "type": error_type}
    if code is not None:
        err["code"] = code
    return {"error": err}


def _call_provider(provider_name, endpoint, body, timeout=120):
    """Call a provider and return (response_dict, status_code).
    Includes retry logic for transient failures (502, 503)."""
    prov = PROVIDERS.get(provider_name)
    if not prov or not prov["key"]:
        return _format_error(
            f"Provider '{provider_name}' not configured or missing API key.",
            "config_error", 503
        ), 503

    url = f"{prov['base']}{endpoint}"
    data = json.dumps(body).encode()

    last_result = None
    last_status = None

    for attempt in range(MAX_RETRIES + 1):
        req = Request(url, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {prov['key']}")
        req.add_header("Content-Type", "application/json")

        # OpenRouter-specific headers
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
                err_body = None

            # Map upstream error codes to OpenAI-compatible format
            status = e.code
            if status == 401:
                return _format_error(
                    "Invalid API key for upstream provider.",
                    "authentication_error", 401
                ), 401
            elif status == 429:
                return _format_error(
                    "Rate limit exceeded for upstream provider. Please try again later.",
                    "rate_limit_error", 429
                ), 429
            elif status in RETRY_STATUS_CODES and attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "Transient %s from %s (attempt %d/%d), retrying in %.1fs",
                    status, provider_name, attempt + 1, MAX_RETRIES, delay,
                )
                time.sleep(delay)
                continue
            elif status == 500:
                return _format_error(
                    "Internal server error from upstream provider.",
                    "upstream_error", 500
                ), 500
            elif status == 502:
                last_result = _format_error(
                    "Bad gateway — upstream provider returned an invalid response.",
                    "upstream_error", 502
                )
                last_status = 502
            elif status == 503:
                last_result = _format_error(
                    "Upstream provider is temporarily unavailable.",
                    "upstream_error", 503
                )
                last_status = 503
            elif status == 504:
                return _format_error(
                    "Upstream provider timed out.",
                    "timeout_error", 504
                ), 504
            else:
                # Pass through the upstream error body if available
                if err_body and "error" in err_body:
                    return err_body, status
                return _format_error(
                    f"Upstream {provider_name} returned HTTP {status}.",
                    "upstream_error", status
                ), status

        except URLError as e:
            last_result = _format_error(
                f"Connection error to {provider_name}: {str(e)[:200]}",
                "connection_error", 502
            )
            last_status = 502

        except Exception as e:
            return _format_error(
                f"Unexpected error calling {provider_name}: {str(e)[:200]}",
                "server_error", 500
            ), 500

    # Exhausted retries
    return last_result, last_status


def _call_provider_streaming(provider_name, endpoint, body, timeout=120):
    """Call a provider in streaming mode. Returns (http_response, status_code) or (error_dict, status).
    The caller is responsible for reading chunks from the response."""
    prov = PROVIDERS.get(provider_name)
    if not prov or not prov["key"]:
        return None, 503, _format_error(
            f"Provider '{provider_name}' not configured or missing API key.",
            "config_error", 503
        )

    url = f"{prov['base']}{endpoint}"
    data = json.dumps(body).encode()

    req = Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {prov['key']}")
    req.add_header("Content-Type", "application/json")

    if provider_name == "openrouter":
        req.add_header("HTTP-Referer", "https://opsora.id")
        req.add_header("X-Title", "Opsora Agent API")

    try:
        resp = urlopen(req, timeout=timeout)
        # Enable incremental reads on POST responses.
        # By default, urlopen sets _method=None for POST, causing read(amt)
        # to buffer the entire body. Setting _method makes it respect amt.
        resp._method = "POST"
        return resp, resp.status, None
    except HTTPError as e:
        try:
            err_body = json.loads(e.read().decode())
        except Exception:
            err_body = None
        status = e.code
        if err_body and "error" in err_body:
            return None, status, err_body
        return None, status, _format_error(
            f"Upstream {provider_name} returned HTTP {status}.",
            "upstream_error", status
        )
    except (URLError, Exception) as e:
        return None, 502, _format_error(
            f"Connection error to {provider_name}: {str(e)[:200]}",
            "connection_error", 502
        )


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


def _route_with_fallback_streaming(model_alias, endpoint, body):
    """Streaming variant. Returns (http_response, status, provider_used, real_model, error_dict_or_none)."""
    config = MODEL_CONFIG.get(model_alias)
    if not config:
        resp, status, err = _call_provider_streaming("nvidia", endpoint, body)
        return resp, status, "nvidia", model_alias, err

    primary_prov, primary_model = config["primary"]
    body["model"] = primary_model
    resp, status, err = _call_provider_streaming(primary_prov, endpoint, body)
    if status == 200 and resp is not None:
        return resp, status, primary_prov, primary_model, None

    for fb_prov, fb_model in config.get("fallbacks", []):
        body["model"] = fb_model
        resp, status, err = _call_provider_streaming(fb_prov, endpoint, body)
        if status == 200 and resp is not None:
            return resp, status, fb_prov, fb_model, None

    return resp, status, primary_prov, primary_model, err


# ---------------------------------------------------------------------------
# Request Validation
# ---------------------------------------------------------------------------

def _validate_chat_request(body):
    """Validate a chat completion request body. Returns (is_valid, error_message)."""
    # messages is required and must be a non-empty array
    messages = body.get("messages")
    if messages is None:
        return False, "'messages' field is required."
    if not isinstance(messages, list) or len(messages) == 0:
        return False, "'messages' must be a non-empty array."

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            return False, f"messages[{i}] must be an object."
        if "role" not in msg:
            return False, f"messages[{i}] is missing 'role' field."
        if "content" not in msg and msg.get("role") != "assistant":
            # Allow assistant messages without content (e.g., tool calls)
            if "tool_calls" not in msg and "function_call" not in msg:
                return False, f"messages[{i}] is missing 'content' field."

    return True, None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("opsora")

# ---------------------------------------------------------------------------
# CORS Headers
# ---------------------------------------------------------------------------

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Max-Age": "86400",
}

# ---------------------------------------------------------------------------
# Request Handler
# ---------------------------------------------------------------------------


class OpsoraHandler(BaseHTTPRequestHandler):
    # Use HTTP/1.1 for proper streaming support (chunked transfer encoding)
    protocol_version = "HTTP/1.1"
    # Increase timeout for streaming
    timeout = 180

    def log_message(self, fmt, *args):
        logger.info(fmt, *args)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None

    def _authenticate(self):
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if OPSORA_API_KEYS and token not in OPSORA_API_KEYS:
            self._send_json(_format_error("Invalid API key.", "authentication_error", 401), 401)
            return None
        return token

    def _send_cors_headers(self):
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Powered-By", "Opsora Agent API")
        self._send_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_sse_chunk(self, chunk_data):
        """Write a single SSE data line and flush immediately."""
        line = f"data: {json.dumps(chunk_data, ensure_ascii=False)}\n\n"
        self.wfile.write(line.encode())
        self.wfile.flush()

    def _send_sse_done(self):
        """Write the SSE termination signal and flush."""
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    # --- GET Routes ---

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
            stats = _get_enhanced_usage_stats(
                api_key=token if OPSORA_API_KEYS else None
            )
            self._send_json(stats)

        elif self.path == "/v1/billing":
            token = self._authenticate()
            if token is None:
                return
            if _billing:
                summary = _billing.get_usage_summary(token)
                self._send_json(summary)
            else:
                self._send_json({"error": "Billing not configured"}, 503)

        elif self.path == "/v1/billing/pricing":
            if _billing:
                self._send_json(_billing.get_pricing_table())
            else:
                self._send_json({"error": "Billing not configured"}, 503)

        elif self.path == "/v1/agent/tools":
            if self._authenticate() is None:
                return
            from tools import get_tool_schemas
            self._send_json({
                "object": "list",
                "data": get_tool_schemas(),
                "count": len(get_tool_schemas()),
            })

        else:
            self._send_json(
                _format_error("Not found.", "invalid_request_error", 404), 404
            )

    # --- POST Routes ---

    def do_POST(self):
        token = self._authenticate()
        if token is None:
            return

        body = self._read_body()
        if body is None:
            self._send_json(
                _format_error("Invalid JSON in request body.", "invalid_request_error", 400), 400
            )
            return

        if self.path in ("/v1/chat/completions", "/v1/completions"):
            self._handle_completions(body, token)
        elif self.path == "/v1/agent/run":
            self._handle_agent_run(body, token)
        elif self.path == "/v1/agent/tools/execute":
            self._handle_tool_execute(body, token)
        else:
            self._send_json(
                _format_error("Not found.", "invalid_request_error", 404), 404
            )

    def _handle_completions(self, body, token):
        is_chat = "chat" in self.path
        model_alias = body.get("model", "opsora-brain")
        endpoint = "/chat/completions" if is_chat else "/completions"

        # --- Request Validation ---
        if is_chat:
            valid, err_msg = _validate_chat_request(body)
            if not valid:
                self._send_json(
                    _format_error(err_msg, "invalid_request_error", 400), 400
                )
                return

        # Validate model alias (known alias or pass-through)
        if model_alias not in MODEL_CONFIG:
            # Allow pass-through — will be sent directly to NVIDIA
            logger.info("Model '%s' not a known alias, passing through to NVIDIA", model_alias)

        body.pop("user", None)
        wants_stream = body.get("stream", False) is True

        # --- Billing quota check ---
        if _billing:
            allowed, billing_info = _billing.check_quota(token, model_alias)
            if not allowed:
                self._send_json(
                    _format_error(
                        billing_info.get("reason", "Quota exceeded") +
                        ". " + billing_info.get("upgrade_hint", ""),
                        "quota_exceeded", 429,
                    ), 429
                )
                return

        logger.info("→ POST %s model=%s stream=%s", self.path, model_alias, wants_stream)
        t0 = time.time()

        # --- Agent Router: opsora-agent ---
        if model_alias == "opsora-agent":
            if wants_stream:
                resp, status, provider, real_model, err = route_agent_streaming(
                    body.get("messages", []), body, _route_with_fallback_streaming
                )
                if status != 200 or resp is None:
                    error_body = err or _format_error("Agent routing failed.", "upstream_error", status)
                    self._send_json(error_body, status)
                    return
                # Stream the response
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Powered-By", "Opsora Agent API")
                self._send_cors_headers()
                self.end_headers()
                self.wfile.flush()
                try:
                    while True:
                        raw = resp.read(4096)
                        if not raw:
                            break
                        self.wfile.write(raw)
                        self.wfile.flush()
                except Exception:
                    pass
                self._send_sse_done()
                try:
                    resp.close()
                except Exception:
                    pass
            else:
                messages = body.get("messages", [])
                result, status, provider, real_model = route_agent_request(
                    messages, body, _route_with_fallback
                )
                if status == 200 and isinstance(result, dict):
                    # Record billing
                    if _billing:
                        usage = result.get("usage", {})
                        _billing.record_usage(
                            token, model_alias,
                            usage.get("prompt_tokens", 0),
                            usage.get("completion_tokens", 0),
                        )
                self._send_json(result, status)

            _log_usage({
                "api_key": token[:8] + "..." if len(token) > 8 else token,
                "model": model_alias,
                "real_model": "opsora-agent",
                "provider": "agent_router",
                "status": status,
                "latency_ms": (time.time() - t0) * 1000,
                "ip": self.client_address[0],
            })
            return

        if wants_stream:
            self._handle_streaming(body, token, model_alias, endpoint, t0)
        else:
            self._handle_non_streaming(body, token, model_alias, endpoint, t0)

    def _handle_non_streaming(self, body, token, model_alias, endpoint, t0):
        result, status, provider, real_model = _route_with_fallback(
            model_alias, endpoint, body
        )
        elapsed = time.time() - t0

        usage = result.get("usage", {}) if isinstance(result, dict) else {}
        input_tok = usage.get("prompt_tokens", 0)
        output_tok = usage.get("completion_tokens", 0)
        total_tok = usage.get("total_tokens", input_tok + output_tok)

        logger.info(
            "← %s → %s (%s) status=%s elapsed=%.2fs tokens=%s",
            model_alias, real_model, provider, status, elapsed, total_tok,
        )

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

        # Record billing
        if _billing and status == 200:
            _billing.record_usage(token, model_alias, input_tok, output_tok)

        # Wrap non-200 errors into OpenAI-compatible format if not already
        if status != 200 and isinstance(result, dict) and "error" not in result:
            result = _format_error(
                f"Upstream error: {json.dumps(result)[:200]}",
                "upstream_error", status
            )

        self._send_json(result, status)

    def _handle_streaming(self, body, token, model_alias, endpoint, t0):
        """Handle streaming (SSE) chat completions."""
        resp, status, provider, real_model, err = _route_with_fallback_streaming(
            model_alias, endpoint, body
        )
        elapsed = time.time() - t0

        if status != 200 or resp is None:
            error_body = err or _format_error(
                "Streaming request failed.", "upstream_error", status
            )
            self._send_json(error_body, status)
            _log_usage({
                "api_key": token[:8] + "..." if len(token) > 8 else token,
                "model": model_alias,
                "real_model": real_model,
                "provider": provider,
                "status": status,
                "latency_ms": elapsed * 1000,
                "ip": self.client_address[0],
            })
            return

        # Send SSE headers
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Powered-By", "Opsora Agent API")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.flush()  # send headers immediately so client knows stream started

        # Generate a completion ID for this stream
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        input_tok = 0
        output_tok = 0
        total_output_chars = 0

        try:
            # Read the upstream SSE stream chunk by chunk
            buffer = ""
            while True:
                raw_chunk = resp.read(4096)
                if not raw_chunk:
                    break
                buffer += raw_chunk.decode("utf-8", errors="replace")

                # Process complete lines
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()

                    if not line or line.startswith(":"):
                        continue  # skip empty / comment lines

                    if line == "data: [DONE]":
                        # Upstream done signal — we'll send our own
                        continue

                    if line.startswith("data: "):
                        payload = line[6:]
                        try:
                            chunk_obj = json.loads(payload)
                        except json.JSONDecodeError:
                            continue

                        # Extract content delta for counting
                        choices = chunk_obj.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            content_piece = delta.get("content", "")
                            if content_piece:
                                total_output_chars += len(content_piece)

                        # Extract upstream usage if present (some providers send it in last chunk)
                        upstream_usage = chunk_obj.get("usage")
                        if upstream_usage:
                            input_tok = upstream_usage.get("prompt_tokens", input_tok)
                            output_tok = upstream_usage.get("completion_tokens", output_tok)

                        # Re-wrap into a clean OpenAI-compatible SSE chunk
                        sse_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": real_model,
                            "choices": choices if choices else [],
                        }
                        self._send_sse_chunk(sse_chunk)

        except Exception as e:
            logger.warning("Stream interrupted: %s", e)

        # Send termination
        self._send_sse_done()
        elapsed = time.time() - t0

        # Estimate output tokens from character count if upstream didn't report
        if output_tok == 0 and total_output_chars > 0:
            output_tok = max(1, total_output_chars // 4)

        logger.info(
            "← [stream] %s → %s (%s) elapsed=%.2fs output_chars=%d",
            model_alias, real_model, provider, elapsed, total_output_chars,
        )

        _log_usage({
            "api_key": token[:8] + "..." if len(token) > 8 else token,
            "model": model_alias,
            "real_model": real_model,
            "provider": provider,
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            "total_tokens": input_tok + output_tok,
            "latency_ms": elapsed * 1000,
            "status": 200,
            "ip": self.client_address[0],
        })

        # Ensure the upstream connection is closed
        try:
            resp.close()
        except Exception:
            pass

    # --- Agent Loop Endpoints ---

    def _handle_agent_run(self, body, token):
        """Run the ReAct agent loop.

        POST /v1/agent/run
        Body: {messages: [...], workspace?: str, model?: str, max_iterations?: int, stream?: bool}
        Returns: {content: str, metadata: {...}, status: int}
        """
        from agent_loop import AgentLoop

        messages = body.get("messages", [])
        if not messages:
            self._send_json(
                _format_error("'messages' field is required and must be non-empty.", "invalid_request_error", 400), 400
            )
            return

        workspace = body.get("workspace", os.getenv("AGENT_WORKSPACE", "/app/workspace"))
        model = body.get("model", "opsora-agent")
        max_iter = body.get("max_iterations", 10)
        wants_stream = body.get("stream", False) is True

        # Billing check
        if _billing:
            allowed, billing_info = _billing.check_quota(token, model)
            if not allowed:
                self._send_json(
                    _format_error(
                        billing_info.get("reason", "Quota exceeded") +
                        ". " + billing_info.get("upgrade_hint", ""),
                        "quota_exceeded", 429,
                    ), 429
                )
                return

        logger.info("→ POST /v1/agent/run model=%s stream=%s max_iter=%d", model, wants_stream, max_iter)
        t0 = time.time()

        agent = AgentLoop(
            model_alias=model,
            workspace=workspace,
            max_iterations=min(int(max_iter), 20),  # Cap at 20
        )

        if wants_stream:
            # Streaming response via SSE
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Powered-By", "Opsora Agent API")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.flush()

            try:
                for event in agent.run_streaming(messages, _route_with_fallback):
                    line = f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    self.wfile.write(line.encode())
                    self.wfile.flush()
            except Exception as e:
                err_event = {"type": "error", "message": str(e)[:500]}
                line = f"data: {json.dumps(err_event)}\n\n"
                self.wfile.write(line.encode())
                self.wfile.flush()

            self._send_sse_done()
        else:
            # Non-streaming response
            result = agent.run(messages, _route_with_fallback)

            elapsed = time.time() - t0
            logger.info(
                "← /v1/agent/run iterations=%d tools=%d latency=%.2fs",
                result["metadata"]["iterations"],
                result["metadata"]["tool_calls"],
                elapsed,
            )

            # Log usage
            _log_usage({
                "api_key": token[:8] + "..." if len(token) > 8 else token,
                "model": model,
                "real_model": result.get("model", "agent-loop"),
                "provider": result.get("provider", "agent-loop"),
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "latency_ms": elapsed * 1000,
                "status": result["status"],
                "ip": self.client_address[0],
            })

            # Format as OpenAI-compatible response
            response = {
                "id": f"agent-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": result.get("model", model),
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": result["content"],
                    },
                    "finish_reason": "stop",
                }],
                "agent_metadata": result["metadata"],
            }
            self._send_json(response, result["status"])

    def _handle_tool_execute(self, body, token):
        """Execute a single tool directly.

        POST /v1/agent/tools/execute
        Body: {tool: str, args: {...}, workspace?: str}
        Returns: {output: str, tool: str}
        """
        from tools import execute_tool, get_tool_names

        tool_name = body.get("tool", "")
        args = body.get("args", {})
        workspace = body.get("workspace", os.getenv("AGENT_WORKSPACE", "/app/workspace"))

        if not tool_name:
            self._send_json(
                _format_error("'tool' field is required.", "invalid_request_error", 400), 400
            )
            return

        if tool_name not in get_tool_names():
            self._send_json(
                _format_error(
                    f"Unknown tool '{tool_name}'. Available: {', '.join(get_tool_names())}",
                    "invalid_request_error", 400,
                ), 400
            )
            return

        logger.info("→ POST /v1/agent/tools/execute tool=%s", tool_name)
        output = execute_tool(tool_name, args, workspace)

        self._send_json({
            "tool": tool_name,
            "output": output,
            "output_length": len(output),
        })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _init_db()

    # Initialize billing engine
    try:
        _billing = BillingEngine(db_path=os.getenv("BILLING_DB_PATH", "billing.db"))
        logger.info("   Billing: enabled (DB: billing.db)")
    except Exception as e:
        logger.warning("   Billing: disabled (%s)", e)

    server = HTTPServer(("0.0.0.0", PORT), OpsoraHandler)
    logger.info("🚀 Opsora Agent API running on port %s", PORT)
    logger.info("   Models: %s", ", ".join(MODEL_CONFIG.keys()))
    logger.info("   Providers: NVIDIA NIM + OpenRouter + DashScope (fallback)")
    logger.info("   Agent Loop: ReAct (Think → Act → Observe), max 10 iterations")
    logger.info("   Agent Tools: read_file, write_file, edit_file, grep_search, glob_search, list_directory, run_command, web_fetch")
    logger.info("   Agent Endpoints: POST /v1/agent/run, GET /v1/agent/tools, POST /v1/agent/tools/execute")
    logger.info("   Auth: %s", "dev mode (no keys)" if not OPSORA_API_KEYS else f"{len(OPSORA_API_KEYS)} key(s)")
    logger.info("   Usage DB: %s", DB_PATH)
    logger.info("   Streaming: SSE enabled")
    logger.info("   Retries: max %d for transient errors (%s)", MAX_RETRIES, RETRY_STATUS_CODES)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.shutdown()
