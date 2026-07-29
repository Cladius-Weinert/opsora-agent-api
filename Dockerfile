# ==============================================================================
# Opsora Agent API — Production Dockerfile
# Single-stage: stdlib-only server, no pip dependencies needed at runtime.
# ==============================================================================

FROM python:3.12-slim

# Security: run as non-root user
RUN groupadd --gid 1001 opsora && \
    useradd --uid 1001 --gid opsora --shell /bin/false --create-home opsora

WORKDIR /app

# Install minimal dependency (dotenv for env var loading)
RUN pip install --no-cache-dir python-dotenv==1.1.0

# Copy application code
COPY opsora_server.py ./
COPY config.py ./
COPY main.py ./

# Copy static assets (landing page + docs served by nginx, but available in container)
COPY index.html docs.html ./
COPY assets/ ./assets/

# Python optimisations
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The app listens on this port; nginx or compose maps it externally.
EXPOSE 8080

# Health check — pure stdlib, no httpx needed
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "from urllib.request import urlopen; r = urlopen('http://localhost:8080/health'); assert r.status == 200" || exit 1

# Drop privileges before starting the server
USER opsora

CMD ["python", "opsora_server.py"]
