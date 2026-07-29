#!/bin/bash
# Opsora Agent API — Fly.io one-command deploy
# Usage: bash scripts/deploy-fly.sh

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

source .fly.env

FLY="/root/.fly/bin/flyctl"

echo "🔐 Checking Fly.io auth..."
$FLY auth whoami || { echo "❌ Not authenticated. Check .fly.env"; exit 1; }

echo "📦 Creating app (if not exists)..."
$FLY apps create opsora-agent-api 2>/dev/null || echo "  App already exists"

echo "💾 Creating persistent volume (1GB, Singapore)..."
$FLY volumes create opsora_data --region sin --size 1 --yes 2>/dev/null || echo "  Volume already exists"

echo "🔑 Setting secrets..."
$FLY secrets set \
  NVIDIA_API_KEY="$NVIDIA_API_KEY" \
  DASHSCOPE_API_KEY="$DASHSCOPE_API_KEY" \
  OPSORA_API_KEYS="$OPSORA_API_KEYS"

echo "🚀 Deploying..."
$FLY deploy

echo ""
echo "✅ Deploy complete!"
echo "   URL: https://opsora-agent-api.fly.dev"
echo "   Health: https://opsora-agent-api.fly.dev/health"
echo ""
echo "📋 Setting GitHub secret for auto-deploy..."
echo "$FLY_API_TOKEN" | gh secret set FLY_API_TOKEN --repo Cladius-Weinert/opsora-agent-api
echo "   GitHub Actions auto-deploy: ✅"
