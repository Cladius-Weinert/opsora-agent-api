"""Opsora Agent API — OpenAI-compatible proxy backed by NVIDIA NIM."""

from __future__ import annotations

import time
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

import config

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("opsora")

# ---------------------------------------------------------------------------
# Rate limiter (sliding-window counter, per API key)
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, rpm: int) -> None:
        self.rpm = rpm
        self._windows: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str) -> bool:
        now = time.time()
        window = self._windows[key]
        # Purge entries older than 60 s
        cutoff = now - 60
        self._windows[key] = [t for t in window if t > cutoff]
        if len(self._windows[key]) >= self.rpm:
            return False
        self._windows[key].append(now)
        return True


limiter = RateLimiter(config.RATE_LIMIT_RPM)

# ---------------------------------------------------------------------------
# HTTP client (shared across requests)
# ---------------------------------------------------------------------------

client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global client
    client = httpx.AsyncClient(
        base_url=config.NVIDIA_BASE_URL,
        headers={
            "Authorization": f"Bearer {config.NVIDIA_API_KEY}",
            "Content-Type": "application/json",
        },
        timeout=httpx.Timeout(config.UPSTREAM_TIMEOUT, connect=10.0),
    )
    logger.info("Opsora Agent API started — upstream %s", config.NVIDIA_BASE_URL)
    yield
    await client.aclose()
    client = None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Opsora Agent API",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Auth + rate-limit middleware
# ---------------------------------------------------------------------------

def _extract_bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": {"message": "Missing or invalid Authorization header.", "type": "auth_error"}},
    )


def _authenticate(request: Request) -> str:
    """Validate the client API key and return it."""
    token = _extract_bearer(request)
    if not config.OPSORA_API_KEYS:
        # No keys configured → allow all (dev mode)
        logger.warning("OPSORA_API_KEYS is empty — allowing all requests (dev mode)")
        return token
    if token not in config.OPSORA_API_KEYS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"message": "Invalid API key.", "type": "auth_error"}},
        )
    return token


def _check_rate_limit(key: str) -> None:
    if not limiter.allow(key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": {"message": "Rate limit exceeded. Try again later.", "type": "rate_limit_error"}},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_model(requested: str) -> str:
    """Map an Opsora alias to a real NVIDIA model id. Pass through unknown names."""
    return config.MODEL_MAP.get(requested, requested)


def _opsora_headers() -> dict[str, str]:
    return {"X-Powered-By": "Opsora Agent API"}


def _strip_internal_keys(body: dict[str, Any]) -> dict[str, Any]:
    """Remove fields that shouldn't be forwarded upstream."""
    for key in ("user",):
        body.pop(key, None)
    return body


def _log_request(method: str, path: str, model: str, stream: bool) -> None:
    logger.info("→ %s %s model=%s stream=%s", method, path, model, stream)


def _log_response(path: str, status_code: int, elapsed: float) -> None:
    logger.info("← %s status=%s elapsed=%.2fs", path, status_code, elapsed)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "opsora-agent-api"}


@app.get("/v1/models")
async def list_models(request: Request):
    _authenticate(request)
    models = [
        {
            "id": alias,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "opsora",
            "permission": [],
        }
        for alias in config.MODEL_MAP
    ]
    return JSONResponse(
        content={"object": "list", "data": models},
        headers=_opsora_headers(),
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    api_key = _authenticate(request)
    _check_rate_limit(api_key)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "Invalid JSON body.", "type": "invalid_request_error"}})

    requested_model = body.get("model", "opsora-brain")
    real_model = _resolve_model(requested_model)
    body["model"] = real_model
    body = _strip_internal_keys(body)
    stream = body.get("stream", False)

    _log_request("POST", "/v1/chat/completions", requested_model, stream)
    t0 = time.time()

    try:
        if stream:
            return await _stream_upstream("/chat/completions", body, t0)
        else:
            return await _non_stream_upstream("/chat/completions", body, t0)
    except httpx.TimeoutException:
        _log_response("/v1/chat/completions", 504, time.time() - t0)
        raise HTTPException(
            status_code=504,
            detail={"error": {"message": "Upstream request timed out.", "type": "timeout_error"}},
        )
    except httpx.HTTPError as exc:
        _log_response("/v1/chat/completions", 502, time.time() - t0)
        logger.error("Upstream error: %s", exc)
        raise HTTPException(
            status_code=502,
            detail={"error": {"message": "Upstream service error.", "type": "upstream_error"}},
        )


@app.post("/v1/completions")
async def completions(request: Request):
    api_key = _authenticate(request)
    _check_rate_limit(api_key)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "Invalid JSON body.", "type": "invalid_request_error"}})

    requested_model = body.get("model", "opsora-brain")
    real_model = _resolve_model(requested_model)
    body["model"] = real_model
    body = _strip_internal_keys(body)
    stream = body.get("stream", False)

    _log_request("POST", "/v1/completions", requested_model, stream)
    t0 = time.time()

    try:
        if stream:
            return await _stream_upstream("/completions", body, t0)
        else:
            return await _non_stream_upstream("/completions", body, t0)
    except httpx.TimeoutException:
        _log_response("/v1/completions", 504, time.time() - t0)
        raise HTTPException(
            status_code=504,
            detail={"error": {"message": "Upstream request timed out.", "type": "timeout_error"}},
        )
    except httpx.HTTPError as exc:
        _log_response("/v1/completions", 502, time.time() - t0)
        logger.error("Upstream error: %s", exc)
        raise HTTPException(
            status_code=502,
            detail={"error": {"message": "Upstream service error.", "type": "upstream_error"}},
        )


# ---------------------------------------------------------------------------
# Upstream helpers
# ---------------------------------------------------------------------------

async def _non_stream_upstream(path: str, body: dict[str, Any], t0: float) -> JSONResponse:
    resp = await client.post(path, json=body)  # type: ignore[union-attr]
    _log_response(path, resp.status_code, time.time() - t0)

    if resp.status_code != 200:
        logger.error("Upstream %s returned %s: %s", path, resp.status_code, resp.text[:500])
        return JSONResponse(
            status_code=resp.status_code,
            content={"error": {"message": f"Upstream error: {resp.status_code}", "type": "upstream_error"}},
            headers=_opsora_headers(),
        )

    data = resp.json()
    return JSONResponse(content=data, headers=_opsora_headers())


async def _stream_upstream(path: str, body: dict[str, Any], t0: float) -> StreamingResponse:
    req = client.build_request("POST", path, json=body)  # type: ignore[union-attr]
    resp = await client.send(req, stream=True)  # type: ignore[union-attr]

    if resp.status_code != 200:
        body_text = await resp.aread()
        await resp.aclose()
        _log_response(path, resp.status_code, time.time() - t0)
        logger.error("Upstream %s stream returned %s: %s", path, resp.status_code, body_text[:500])
        return JSONResponse(
            status_code=resp.status_code,
            content={"error": {"message": f"Upstream error: {resp.status_code}", "type": "upstream_error"}},
            headers=_opsora_headers(),
        )

    async def event_generator():
        try:
            async for line in resp.aiter_lines():
                if line:
                    yield line + "\n"
        finally:
            await resp.aclose()
            _log_response(path, 200, time.time() - t0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=_opsora_headers(),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, log_level="info")
