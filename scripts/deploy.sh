#!/usr/bin/env bash
# ==============================================================================
# Opsora Agent API — Deploy Script
# Pulls latest image from GHCR, stops old container, starts new one,
# runs health check, and rolls back on failure.
#
# Usage: ./scripts/deploy.sh [--rollback]
# ==============================================================================

set -euo pipefail

# ---- Configuration ----
IMAGE_NAME="ghcr.io/opsora-ai/opsora-agent-api"
IMAGE_TAG="${1:-latest}"
CONTAINER_NAME="opsora-agent-api"
PROJECT_DIR="/opt/opsora-agent-api"
HEALTH_URL="http://localhost:8080/health"
HEALTH_RETRIES=12
HEALTH_INTERVAL=5
ROLLBACK_TAG="previous"

# ---- Colors for output ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log()   { echo -e "${BLUE}[DEPLOY]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*"; }

# ---- Rollback function ----
rollback() {
    fail "Deployment failed! Rolling back..."

    # Check if the rollback image exists
    if docker image inspect "${IMAGE_NAME}:${ROLLBACK_TAG}" &>/dev/null; then
        log "Restoring previous image..."
        docker tag "${IMAGE_NAME}:${ROLLBACK_TAG}" "${IMAGE_NAME}:latest"
        cd "$PROJECT_DIR"
        docker compose up -d --force-recreate opsora-agent-api
        log "Rollback complete. Verifying..."
        sleep 5
        if check_health; then
            ok "Rollback successful — previous version is running."
        else
            fail "Rollback also failed. Manual intervention required!"
            fail "Check logs: docker logs ${CONTAINER_NAME}"
            exit 1
        fi
    else
        fail "No previous image found for rollback. Manual intervention required!"
        fail "Check logs: docker logs ${CONTAINER_NAME}"
        exit 1
    fi
}

# ---- Health check function ----
check_health() {
    log "Running health check against ${HEALTH_URL} ..."
    for i in $(seq 1 "$HEALTH_RETRIES"); do
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$HEALTH_URL" 2>/dev/null || echo "000")
        if [ "$HTTP_CODE" = "200" ]; then
            ok "Health check passed (HTTP ${HTTP_CODE})"
            return 0
        fi
        warn "  attempt ${i}/${HEALTH_RETRIES} — HTTP ${HTTP_CODE}"
        sleep "$HEALTH_INTERVAL"
    done
    fail "Health check failed after $((HEALTH_RETRIES * HEALTH_INTERVAL))s"
    return 1
}

# ---- Handle --rollback flag ----
if [ "${1:-}" = "--rollback" ]; then
    log "Manual rollback requested."
    rollback
    exit 0
fi

# ---- Pre-flight checks ----
log "Starting deployment of ${IMAGE_NAME}:${IMAGE_TAG}"

if ! command -v docker &>/dev/null; then
    fail "Docker is not installed or not in PATH."
    exit 1
fi

if ! docker compose version &>/dev/null; then
    fail "Docker Compose V2 is not available."
    exit 1
fi

cd "$PROJECT_DIR"

# ---- Tag current image as rollback point ----
if docker image inspect "${IMAGE_NAME}:latest" &>/dev/null; then
    log "Tagging current image as rollback point..."
    docker tag "${IMAGE_NAME}:latest" "${IMAGE_NAME}:${ROLLBACK_TAG}"
fi

# ---- Pull latest image ----
log "Pulling latest image..."
docker compose pull opsora-agent-api

# ---- Stop old container, start new one ----
log "Starting new container..."
docker compose up -d --remove-orphans opsora-agent-api

# ---- Prune old images ----
log "Cleaning up dangling images..."
docker image prune -f

# ---- Health check ----
if check_health; then
    ok "🚀 Deployment successful!"
    log "Container status:"
    docker ps --filter "name=${CONTAINER_NAME}" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    # Clean up rollback image on success
    docker rmi "${IMAGE_NAME}:${ROLLBACK_TAG}" 2>/dev/null || true
else
    rollback
    exit 1
fi
