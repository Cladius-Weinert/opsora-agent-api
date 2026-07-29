"""Configuration for Opsora Agent API."""

import os
from dotenv import load_dotenv

load_dotenv()

# NVIDIA NIM backend
NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1/")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")

# Client authentication — comma-separated list of valid API keys
OPSORA_API_KEYS = [
    k.strip()
    for k in os.getenv("OPSORA_API_KEYS", "").split(",")
    if k.strip()
]

# Custom model name -> NVIDIA NIM model id
MODEL_MAP: dict[str, str] = {
    "opsora-fast": "deepseek-ai/deepseek-v4-flash",
    "opsora-brain": "meta/llama-3.1-70b-instruct",
    "opsora-code": "nvidia/llama-3.3-nemotron-super-49b-v1",
    "opsora-vision": "meta/llama-3.2-90b-vision-instruct",
    "opsora-reason": "deepseek-ai/deepseek-v4-pro",
    "opsora-max": "nvidia/nemotron-3-ultra-550b-a55b",
}

# Rate limiting (requests per minute per API key)
RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "60"))

# Request timeout to upstream (seconds)
UPSTREAM_TIMEOUT = int(os.getenv("UPSTREAM_TIMEOUT", "120"))

# Server
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
