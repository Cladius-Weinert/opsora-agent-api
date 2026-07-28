# ==============================================================================
# Opsora Agent API — Production Dockerfile
# Multi-stage build: builder (compile deps) → runtime (slim, non-root)
# ==============================================================================

# ---- Stage 1: Builder ----
# Install dependencies in a full Python image, then copy the virtualenv to runtime.
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools (some packages may need compilers)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

# Copy only requirements first to maximise Docker layer caching.
# Any change to app code won't invalidate this layer.
COPY requirements.txt .

# Create a virtualenv so we can copy it cleanly to the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ---- Stage 2: Runtime ----
# Minimal image with only the app + virtualenv. No compilers, no pip, no apt cache.
FROM python:3.12-slim AS runtime

# Security: run as non-root user
RUN groupadd --gid 1001 opsora && \
    useradd --uid 1001 --gid opsora --shell /bin/false --create-home opsora

WORKDIR /app

# Copy the pre-built virtualenv from the builder stage
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code
COPY main.py config.py ./

# Copy static assets (landing page + docs served by nginx, but available in container)
COPY index.html docs.html ./
COPY assets/ ./assets/

# Python optimisations
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# The app listens on this port; nginx or compose maps it externally.
EXPOSE 8080

# Health check — lightweight, no extra dependency needed (httpx is already in venv)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; r = httpx.get('http://localhost:8080/health'); r.raise_for_status()" || exit 1

# Drop privileges before starting the server
USER opsora

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info", "--access-log"]
