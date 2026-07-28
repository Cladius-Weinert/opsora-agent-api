# Opsora Agent API

OpenAI-compatible API gateway powered by NVIDIA NIM. Lightweight, production-ready proxy designed for cheap VPS deployment.

## Features

- **OpenAI-compatible endpoints** — drop-in replacement for `/v1/chat/completions`, `/v1/completions`, `/v1/models`
- **Streaming support** — SSE streaming for real-time responses
- **Custom model aliases** — branded model names mapped to NVIDIA NIM models
- **API key authentication** — Bearer token auth with configurable keys
- **Rate limiting** — sliding-window per-key rate limiter
- **Structured logging** — request/response logging with timing
- **Docker-ready** — 256 MB memory limit, health checks included

## Model Mapping

| Opsora Model | Backend Model |
|---|---|
| `opsora-brain` | `meta/llama-3.1-70b-instruct` |
| `opsora-fast` | `deepseek-ai/deepseek-v4-flash` |
| `opsora-code` | `meta/llama-3.1-70b-instruct` |
| `opsora-vision` | `google/gemma-4-31b-it` |
| `opsora-reason` | `deepseek-ai/deepseek-v4-pro` |
| `opsora-max` | `nvidia/llama-3.1-nemotron-ultra-253b` |

## Quick Start

### Docker (recommended)

```bash
cp .env.example .env
# Edit .env with your NVIDIA API key and Opsora API keys
docker compose up -d
```

### Manual

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env
python main.py
```

## Usage

```bash
curl https://your-server:8080/v1/chat/completions \
  -H "Authorization: Bearer opsk-your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "opsora-brain",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

Streaming:

```bash
curl https://your-server:8080/v1/chat/completions \
  -H "Authorization: Bearer opsk-your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "opsora-fast",
    "messages": [{"role": "user", "content": "Write a haiku"}],
    "stream": true
  }'
```

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Description | Default |
|---|---|---|
| `NVIDIA_API_KEY` | NVIDIA NIM API key | *(required)* |
| `OPSORA_API_KEYS` | Comma-separated client API keys | *(empty = dev mode)* |
| `RATE_LIMIT_RPM` | Requests per minute per key | `60` |
| `UPSTREAM_TIMEOUT` | Upstream timeout in seconds | `120` |
| `HOST` | Bind address | `0.0.0.0` |
| `PORT` | Bind port | `8080` |

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Health check (no auth) |
| GET | `/v1/models` | List available models |
| POST | `/v1/chat/completions` | Chat completions (streaming supported) |
| POST | `/v1/completions` | Text completions (streaming supported) |
