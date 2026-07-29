#!/usr/bin/env python3
"""Opsora MongoDB Store — Agent memory and conversation history.

Uses MongoDB Atlas (free tier: 512MB shared cluster) for:
- Conversation history persistence
- Agent session memory (tool calls, results, metadata)
- Long-term knowledge storage

Stdlib-only for HTTP calls. Uses MongoDB Data API (HTTPS)
instead of pymongo driver to avoid dependencies.

Zero external dependencies — Python stdlib only.
"""

import json
import logging
import os
import time
import uuid
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

logger = logging.getLogger("opsora.mongo")

# MongoDB Atlas Data API configuration
MONGO_DATA_API_URL = os.getenv(
    "MONGO_DATA_API_URL",
    "https://data.mongodb-api.com/app/data-0-irfki/endpoint/action",
)
MONGO_API_KEY = os.getenv("MONGO_API_KEY", "")
MONGO_CLUSTER = os.getenv("MONGO_CLUSTER", "")
MONGO_DB = os.getenv("MONGO_DB", "opsora")


def _is_configured():
    """Check if MongoDB is configured."""
    return bool(MONGO_API_KEY and MONGO_CLUSTER)


def _call_api(action, payload):
    """Call MongoDB Data API."""
    if not _is_configured():
        return None, "MongoDB not configured (MONGO_API_KEY or MONGO_CLUSTER missing)"

    url = f"{MONGO_DATA_API_URL}/{action}"
    data = json.dumps(payload).encode()
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("api-key", MONGO_API_KEY)

    try:
        resp = urlopen(req, timeout=10)
        return json.loads(resp.read().decode()), None
    except HTTPError as e:
        body = e.read().decode()[:500]
        return None, f"MongoDB API error {e.code}: {body}"
    except (URLError, Exception) as e:
        return None, f"MongoDB connection error: {str(e)[:300]}"


# ---------------------------------------------------------------------------
# Conversation History
# ---------------------------------------------------------------------------

def save_conversation(conversation_id, messages, metadata=None):
    """Save or update a conversation in MongoDB.

    Args:
        conversation_id: Unique conversation identifier
        messages: List of chat messages (OpenAI format)
        metadata: Optional dict with extra metadata (model, provider, etc.)

    Returns:
        (success: bool, error: str or None)
    """
    if not _is_configured():
        return False, "MongoDB not configured"

    payload = {
        "dataSource": MONGO_CLUSTER,
        "database": MONGO_DB,
        "collection": "conversations",
        "filter": {"_id": conversation_id},
        "update": {
            "$set": {
                "messages": messages,
                "metadata": metadata or {},
                "updated_at": time.time(),
                "message_count": len(messages),
            },
            "$setOnInsert": {
                "_id": conversation_id,
                "created_at": time.time(),
            },
        },
        "upsert": True,
    }

    result, err = _call_api("updateOne", payload)
    if err:
        logger.error("save_conversation failed: %s", err)
        return False, err
    return True, None


def load_conversation(conversation_id):
    """Load a conversation from MongoDB.

    Returns:
        (messages: list or None, error: str or None)
    """
    if not _is_configured():
        return None, "MongoDB not configured"

    payload = {
        "dataSource": MONGO_CLUSTER,
        "database": MONGO_DB,
        "collection": "conversations",
        "filter": {"_id": conversation_id},
    }

    result, err = _call_api("findOne", payload)
    if err:
        return None, err
    if not result or not result.get("document"):
        return None, None  # Not found
    return result["document"].get("messages", []), None


def list_conversations(limit=20, offset=0):
    """List recent conversations.

    Returns:
        (conversations: list, error: str or None)
    """
    if not _is_configured():
        return [], "MongoDB not configured"

    payload = {
        "dataSource": MONGO_CLUSTER,
        "database": MONGO_DB,
        "collection": "conversations",
        "sort": {"updated_at": -1},
        "skip": offset,
        "limit": limit,
        "projection": {
            "_id": 1,
            "created_at": 1,
            "updated_at": 1,
            "message_count": 1,
            "metadata.model": 1,
        },
    }

    result, err = _call_api("find", payload)
    if err:
        return [], err
    return result.get("documents", []), None


# ---------------------------------------------------------------------------
# Agent Session Memory
# ---------------------------------------------------------------------------

def save_agent_session(session_id, goal, steps, result, metadata=None):
    """Save an agent execution session.

    Args:
        session_id: Unique session identifier
        goal: The original user goal
        steps: List of executed steps with tool calls and results
        result: Final agent response
        metadata: Optional metadata (iterations, latency, tools used)
    """
    if not _is_configured():
        return False, "MongoDB not configured"

    payload = {
        "dataSource": MONGO_CLUSTER,
        "database": MONGO_DB,
        "collection": "agent_sessions",
        "document": {
            "_id": session_id,
            "goal": goal,
            "steps": steps,
            "result": result,
            "metadata": metadata or {},
            "created_at": time.time(),
        },
    }

    result_data, err = _call_api("insertOne", payload)
    if err:
        logger.error("save_agent_session failed: %s", err)
        return False, err
    return True, None


def search_agent_sessions(query, limit=10):
    """Search agent sessions by goal text.

    Returns:
        (sessions: list, error: str or None)
    """
    if not _is_configured():
        return [], "MongoDB not configured"

    payload = {
        "dataSource": MONGO_CLUSTER,
        "database": MONGO_DB,
        "collection": "agent_sessions",
        "filter": {
            "goal": {"$regex": query, "$options": "i"},
        },
        "sort": {"created_at": -1},
        "limit": limit,
        "projection": {
            "_id": 1,
            "goal": 1,
            "metadata.iterations": 1,
            "metadata.tools_used": 1,
            "created_at": 1,
        },
    }

    result, err = _call_api("find", payload)
    if err:
        return [], err
    return result.get("documents", []), None


# ---------------------------------------------------------------------------
# Usage Analytics (for Elastic sync)
# ---------------------------------------------------------------------------

def save_usage_event(event):
    """Save a usage event for analytics.

    Args:
        event: Dict with api_key, model, tokens, latency, status, etc.
    """
    if not _is_configured():
        return False, "MongoDB not configured"

    event["_id"] = str(uuid.uuid4())
    event["timestamp"] = time.time()

    payload = {
        "dataSource": MONGO_CLUSTER,
        "database": MONGO_DB,
        "collection": "usage_events",
        "document": event,
    }

    result, err = _call_api("insertOne", payload)
    if err:
        return False, err
    return True, None


def get_usage_analytics(days=7):
    """Get aggregated usage analytics.

    Returns:
        (analytics: dict, error: str or None)
    """
    if not _is_configured():
        return {}, "MongoDB not configured"

    cutoff = time.time() - (days * 86400)

    payload = {
        "dataSource": MONGO_CLUSTER,
        "database": MONGO_DB,
        "collection": "usage_events",
        "pipeline": [
            {"$match": {"timestamp": {"$gte": cutoff}}},
            {"$group": {
                "_id": "$model",
                "total_requests": {"$sum": 1},
                "total_tokens": {"$sum": "$total_tokens"},
                "avg_latency": {"$avg": "$latency_ms"},
            }},
            {"$sort": {"total_requests": -1}},
        ],
    }

    result, err = _call_api("aggregate", payload)
    if err:
        return {}, err
    return result.get("documents", []), None


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------

def health_check():
    """Check MongoDB connectivity.

    Returns:
        (status: dict, error: str or None)
    """
    if not _is_configured():
        return {"configured": False}, None

    payload = {
        "dataSource": MONGO_CLUSTER,
        "database": MONGO_DB,
        "collection": "conversations",
        "filter": {},
        "limit": 1,
    }

    result, err = _call_api("findOne", payload)
    if err:
        return {"configured": True, "connected": False}, err

    return {
        "configured": True,
        "connected": True,
        "cluster": MONGO_CLUSTER,
        "database": MONGO_DB,
    }, None
