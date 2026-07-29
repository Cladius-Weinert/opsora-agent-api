#!/usr/bin/env python3
"""Sync usage data from Opsora Agent API to Elastic Cloud.

Reads usage stats from the API and indexes them in Elasticsearch
for search, analytics, and Kibana dashboards.

Stdlib-only — no elasticsearch-py dependency needed.
"""

import json
import os
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# Configuration
ELASTIC_ENDPOINT = os.getenv("ELASTIC_ENDPOINT", "")
ELASTIC_API_KEY = os.getenv("ELASTIC_CLOUD_API_KEY", "")
RENDER_API_URL = os.getenv("RENDER_API_URL", "https://opsora-agent-api.onrender.com")
OPSORA_API_TOKEN = os.getenv("OPSORA_API_TOKEN", "")

INDEX_NAME = "opsora-usage"


def elastic_request(method, path, body=None):
    """Make a request to Elasticsearch."""
    if not ELASTIC_ENDPOINT or not ELASTIC_API_KEY:
        print("ERROR: ELASTIC_ENDPOINT and ELASTIC_CLOUD_API_KEY required")
        sys.exit(1)

    url = f"{ELASTIC_ENDPOINT}/{path}"
    data = json.dumps(body).encode() if body else None

    req = Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"ApiKey {ELASTIC_API_KEY}")

    try:
        resp = urlopen(req, timeout=15)
        return json.loads(resp.read().decode()), resp.status
    except HTTPError as e:
        body_text = e.read().decode()[:500]
        print(f"Elastic error {e.code}: {body_text}")
        return None, e.code
    except (URLError, Exception) as e:
        print(f"Elastic connection error: {e}")
        return None, 0


def fetch_api_usage():
    """Fetch usage data from the Opsora Agent API."""
    url = f"{RENDER_API_URL}/v1/usage"
    req = Request(url)
    if OPSORA_API_TOKEN:
        req.add_header("Authorization", f"Bearer {OPSORA_API_TOKEN}")

    try:
        resp = urlopen(req, timeout=15)
        return json.loads(resp.read().decode())
    except Exception as e:
        print(f"API fetch error: {e}")
        return None


def ensure_index():
    """Create the Elasticsearch index if it doesn't exist."""
    result, status = elastic_request("GET", f"{INDEX_NAME}", None)
    if status == 200:
        print(f"Index '{INDEX_NAME}' already exists")
        return True

    # Create index with mapping
    mapping = {
        "mappings": {
            "properties": {
                "timestamp": {"type": "date"},
                "model": {"type": "keyword"},
                "provider": {"type": "keyword"},
                "total_requests": {"type": "integer"},
                "total_tokens": {"type": "long"},
                "avg_latency_ms": {"type": "float"},
                "input_tokens": {"type": "long"},
                "output_tokens": {"type": "long"},
                "status": {"type": "keyword"},
                "sync_source": {"type": "keyword"},
            }
        },
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
        },
    }

    result, status = elastic_request("PUT", INDEX_NAME, mapping)
    if status in (200, 201):
        print(f"Created index '{INDEX_NAME}'")
        return True
    print(f"Failed to create index: {result}")
    return False


def sync_usage(usage_data):
    """Sync usage data to Elasticsearch."""
    if not usage_data:
        print("No usage data to sync")
        return

    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Build document
    doc = {
        "timestamp": timestamp,
        "sync_source": "github-actions",
        "total_requests": usage_data.get("total_requests", 0),
        "total_tokens": usage_data.get("total_tokens", 0),
        "models": usage_data.get("models", []),
        "top_models": usage_data.get("top_models", []),
        "periods": usage_data.get("periods", {}),
    }

    result, status = elastic_request("POST", f"{INDEX_NAME}/_doc", doc)
    if status in (200, 201):
        print(f"✅ Synced usage to Elastic: {doc['total_requests']} requests, {doc['total_tokens']} tokens")
    else:
        print(f"❌ Failed to sync usage: HTTP {status}")


def main():
    print("=" * 60)
    print("Opsora → Elastic Cloud Usage Sync")
    print("=" * 60)

    # Step 1: Ensure index exists
    print("\n[1] Checking Elasticsearch index...")
    if not ensure_index():
        sys.exit(1)

    # Step 2: Fetch usage from API
    print("\n[2] Fetching usage data from API...")
    usage = fetch_api_usage()
    if not usage:
        print("Could not fetch usage data from API")
        sys.exit(1)
    print(f"   Got: {usage.get('total_requests', 0)} total requests")

    # Step 3: Sync to Elastic
    print("\n[3] Syncing to Elasticsearch...")
    sync_usage(usage)

    # Step 4: Verify
    print("\n[4] Verifying...")
    result, status = elastic_request("GET", f"{INDEX_NAME}/_count", None)
    if status == 200:
        count = result.get("count", 0)
        print(f"   Index has {count} documents")

    print("\n✅ Sync complete!")


if __name__ == "__main__":
    main()
