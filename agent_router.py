#!/usr/bin/env python3
"""Opsora Agent Router — Intelligent multi-model request routing.

Automatically detects user intent, selects the optimal model, decomposes
complex requests into subtasks, and synthesizes results. Exposed as
model alias 'opsora-agent' in the API gateway.

Zero external dependencies — Python stdlib only."""

import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger("opsora")

# ---------------------------------------------------------------------------
# Intent Detection Keywords
# ---------------------------------------------------------------------------

CODE_KEYWORDS = {
    "code", "function", "class", "bug", "debug", "refactor", "test",
    "implement", "program", "script", "api", "database", "sql",
    "python", "javascript", "typescript", "html", "css", "docker",
    "git", "regex", "algorithm", "deploy", "server", "endpoint",
    "middleware", "import", "export", "variable", "loop", "array",
    "object", "compile", "syntax", "runtime", "stack trace",
    "exception", "error handling", "unit test", "integration test",
    "lint", "format", "package", "module", "library", "framework",
    "react", "node", "express", "django", "flask", "fastapi",
    "async", "await", "promise", "callback", "closure", "decorator",
    "inheritance", "polymorphism", "interface", "abstract", "enum",
    "struct", "pointer", "memory", "thread", "process", "socket",
    "http", "rest", "graphql", "websocket", "grpc", "microservice",
    "kubernetes", "terraform", "ci/cd", "pipeline", "migration",
    "schema", "query", "index", "join", "foreign key", "normaliz",
}

VISION_KEYWORDS = {
    "image", "picture", "photo", "screenshot", "diagram", "chart",
    "visual", "see", "look at", "describe this image", "what's in this",
    "ocr", "scan", "recognize", "detect object", "face", "color",
    "layout", "ui design", "mockup", "wireframe", "pixel", "resolution",
    "figure", "graph", "plot", "infographic", "map", "floor plan",
    "logo", "icon", "banner", "thumbnail", "video frame",
}

REASONING_KEYWORDS = {
    "explain why", "prove", "analyze", "compare", "logic", "math",
    "calculate", "step by step", "reasoning", "deduce", "theorem",
    "equation", "solve", "derive", "formula", "proof", "hypothesis",
    "conclusion", "implication", "paradox", "fallacy", "premise",
    "syllogism", "induction", "contradiction", "probability",
    "statistics", "mean", "median", "variance", "regression",
    "correlation", "distribution", "combinatorics", "optimization",
    "trade-off", "pros and cons", "advantages", "disadvantages",
    "which is better", "should i", "evaluate", "assess",
    "risk", "impact", "consequence", "worst case", "best case",
}

CREATIVE_KEYWORDS = {
    "write", "create", "compose", "story", "poem", "marketing",
    "copy", "slogan", "brainstorm", "ideas", "blog post",
    "email draft", "tagline", "headline", "caption", "script",
    "dialogue", "narrative", "fiction", "novel", "essay",
    "article", "review", "description", "pitch", "proposal",
    "presentation", "speech", "toast", "eulogy", "song",
    "lyrics", "joke", "pun", "haiku", "limerick",
    "catchy", "engaging", "compelling", "persuasive",
}

# Intent categories and their associated models
INTENT_MODEL_MAP = {
    "code": "opsora-code",
    "vision": "opsora-vision",
    "reasoning": "opsora-reason",
    "creative": "opsora-brain",
    "general": "opsora-fast",
}

# Minimum keyword matches to classify an intent
MIN_MATCH_THRESHOLD = 2

# ---------------------------------------------------------------------------
# Intent Detection
# ---------------------------------------------------------------------------


def _extract_last_user_message(messages):
    """Get the text of the last user message."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                return " ".join(text_parts)
            return str(content) if content else ""
    return ""


def _count_keyword_matches(text, keywords):
    """Count how many keywords from a set appear in the text."""
    text_lower = text.lower()
    matches = 0
    for kw in keywords:
        if " " in kw:
            if kw in text_lower:
                matches += 1
        else:
            if re.search(r'\b' + re.escape(kw) + r'\b', text_lower):
                matches += 1
    return matches


def detect_intent(messages):
    """Classify the user's intent from the message history.

    Returns one of: 'code', 'vision', 'reasoning', 'creative', 'general', 'mixed'.
    Uses keyword-based heuristics for speed (no extra API call needed).
    """
    text = _extract_last_user_message(messages)
    if not text:
        return "general"

    scores = {}
    keyword_sets = {
        "code": CODE_KEYWORDS,
        "vision": VISION_KEYWORDS,
        "reasoning": REASONING_KEYWORDS,
        "creative": CREATIVE_KEYWORDS,
    }

    for intent, keywords in keyword_sets.items():
        score = _count_keyword_matches(text, keywords)
        if score > 0:
            scores[intent] = score

    if not scores:
        return "general"

    sorted_intents = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_intent, top_score = sorted_intents[0]

    if top_score < MIN_MATCH_THRESHOLD:
        return "general"

    # Check if multiple intents have strong scores (mixed)
    if len(sorted_intents) >= 2:
        second_intent, second_score = sorted_intents[1]
        if second_score >= MIN_MATCH_THRESHOLD and second_score >= top_score * 0.5:
            logger.info("Agent Router: mixed intent detected — %s (%d) + %s (%d)",
                        top_intent, top_score, second_intent, second_score)
            return "mixed"

    logger.info("Agent Router: intent=%s (score=%d)", top_intent, top_score)
    return top_intent


# ---------------------------------------------------------------------------
# Task Decomposition
# ---------------------------------------------------------------------------


def decompose_task(messages, intent):
    """Break a complex request into subtasks for mixed-intent routing.

    Returns a list of subtask dicts:
    [{"intent": "code", "model": "opsora-code", "prompt_suffix": "..."}, ...]

    For non-mixed intents, returns a single-element list.
    """
    if intent != "mixed":
        model = select_model(intent)
        return [{"intent": intent, "model": model, "prompt_suffix": ""}]

    text = _extract_last_user_message(messages)
    subtasks = []

    # Heuristic decomposition: look for distinct action patterns
    patterns = [
        (r'(?:explain|describe|what is|how does).{10,80}(?:code|function|class|api)',
         "code", "Explain the code"),
        (r'(?:write|create|implement|build).{10,80}(?:code|function|class|test|api)',
         "code", "Write the code"),
        (r'(?:analyze|evaluate|compare|assess).{10,80}',
         "reasoning", "Analyze the topic"),
        (r'(?:write|create|compose|draft).{10,80}(?:blog|email|article|copy|story)',
         "creative", "Create the content"),
        (r'(?:look at|describe|explain).{10,80}(?:image|photo|diagram|screenshot)',
         "vision", "Analyze the visual"),
    ]

    seen_intents = set()
    for pattern, sub_intent, suffix in patterns:
        if re.search(pattern, text, re.IGNORECASE) and sub_intent not in seen_intents:
            model = select_model(sub_intent)
            subtasks.append({
                "intent": sub_intent,
                "model": model,
                "prompt_suffix": suffix,
            })
            seen_intents.add(sub_intent)

    # If decomposition didn't produce subtasks, default to two strong models
    if not subtasks:
        scores = {}
        for intent_name, keywords in {
            "code": CODE_KEYWORDS,
            "reasoning": REASONING_KEYWORDS,
            "creative": CREATIVE_KEYWORDS,
        }.items():
            score = _count_keyword_matches(text, keywords)
            if score > 0:
                scores[intent_name] = score

        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        for intent_name, _ in sorted_scores[:2]:
            model = select_model(intent_name)
            subtasks.append({
                "intent": intent_name,
                "model": model,
                "prompt_suffix": f"Focus on {intent_name} aspects",
            })

    # Fallback: always ensure at least one subtask
    if not subtasks:
        subtasks.append({
            "intent": "general",
            "model": "opsora-brain",
            "prompt_suffix": "",
        })

    logger.info("Agent Router: decomposed into %d subtasks: %s",
                len(subtasks), [s["intent"] for s in subtasks])
    return subtasks


# ---------------------------------------------------------------------------
# Model Selection
# ---------------------------------------------------------------------------


def select_model(intent):
    """Pick the best model alias for a given intent."""
    return INTENT_MODEL_MAP.get(intent, "opsora-brain")


# ---------------------------------------------------------------------------
# Parallel Execution
# ---------------------------------------------------------------------------


def _execute_subtask(subtask, messages, body, route_fn):
    """Execute a single subtask by calling the route function.

    Args:
        subtask: dict with 'intent', 'model', 'prompt_suffix'
        messages: original message list
        body: original request body
        route_fn: _route_with_fallback from opsora_server.py

    Returns:
        dict with 'intent', 'model', 'content', 'provider', 'real_model', 'status'
    """
    model_alias = subtask["model"]
    task_body = dict(body)
    task_body["model"] = model_alias
    task_body["stream"] = False  # Subtasks always non-streaming for synthesis

    if subtask.get("prompt_suffix"):
        # Append focus instruction to the last user message
        task_messages = list(messages)
        if task_messages:
            last_msg = dict(task_messages[-1])
            last_msg["content"] = last_msg.get("content", "") + \
                f"\n\n[Focus: {subtask['prompt_suffix']}]"
            task_messages[-1] = last_msg
        task_body["messages"] = task_messages

    try:
        result, status, provider, real_model = route_fn(
            model_alias, "/chat/completions", task_body
        )

        content = ""
        if status == 200 and isinstance(result, dict):
            choices = result.get("choices", [])
            if choices and choices[0].get("message"):
                content = choices[0]["message"].get("content", "")

        return {
            "intent": subtask["intent"],
            "model": model_alias,
            "content": content,
            "provider": provider,
            "real_model": real_model,
            "status": status,
        }
    except Exception as e:
        logger.error("Agent Router: subtask %s failed: %s", subtask["intent"], e)
        return {
            "intent": subtask["intent"],
            "model": model_alias,
            "content": f"[Error: {str(e)[:200]}]",
            "provider": "error",
            "real_model": "error",
            "status": 500,
        }


# ---------------------------------------------------------------------------
# Result Synthesis
# ---------------------------------------------------------------------------


def _synthesize_results(subtask_results, original_messages):
    """Merge multi-model outputs into a coherent response.

    For single subtask: return the result as-is.
    For multiple subtasks: concatenate with section headers.
    """
    if len(subtask_results) == 1:
        only = subtask_results[0]
        if only["status"] != 200:
            # Failed single subtask would otherwise surface an empty body.
            return "I encountered errors processing your request. Please try rephrasing."
        return only["content"]

    # Sort by intent priority: reasoning > code > vision > creative > general
    priority = {"reasoning": 0, "code": 1, "vision": 2, "creative": 3, "general": 4}
    sorted_results = sorted(
        subtask_results,
        key=lambda r: priority.get(r["intent"], 5)
    )

    intent_labels = {
        "code": "💻 Code Analysis",
        "vision": "👁️ Visual Analysis",
        "reasoning": "🧠 Reasoning & Analysis",
        "creative": "✍️ Creative Content",
        "general": "📋 General Response",
    }

    sections = []
    for result in sorted_results:
        if result["status"] != 200:
            continue
        label = intent_labels.get(result["intent"], result["intent"].title())
        sections.append(f"## {label}\n\n{result['content']}")

    if not sections:
        # All subtasks failed
        return "I encountered errors processing your request. Please try rephrasing."

    return "\n\n---\n\n".join(sections)


# ---------------------------------------------------------------------------
# Main Router Entry Point
# ---------------------------------------------------------------------------


def route_agent_request(messages, body, route_fn, stream=False):
    """Main entry point for opsora-agent routing.

    Args:
        messages: list of chat messages
        body: full request body dict
        route_fn: _route_with_fallback from opsora_server.py
                  signature: (model_alias, endpoint, body) -> (result, status, provider, real_model)
        stream: bool — if True, returns streaming-compatible result

    Returns:
        For non-streaming:
            (result_dict, status, provider, real_model)
            result_dict includes agent_metadata in the response.
        For streaming:
            Falls back to single-model routing (streaming synthesis not supported).
    """
    t0 = time.time()

    # For streaming, just route to the best single model
    if stream:
        intent = detect_intent(messages)
        model = select_model(intent)
        logger.info("Agent Router [stream]: intent=%s → model=%s", intent, model)

        result, status, provider, real_model = route_fn(
            model, "/chat/completions", body
        )
        elapsed = time.time() - t0

        # Add agent metadata to streaming model name
        return result, status, provider, real_model

    # Non-streaming: full agent routing pipeline
    intent = detect_intent(messages)
    logger.info("Agent Router: detected intent=%s", intent)

    if intent == "mixed":
        subtasks = decompose_task(messages, intent)
    else:
        model = select_model(intent)
        subtasks = [{"intent": intent, "model": model, "prompt_suffix": ""}]

    # Execute subtasks
    subtask_results = []
    if len(subtasks) == 1:
        # Single subtask — no need for threads
        result = _execute_subtask(subtasks[0], messages, body, route_fn)
        subtask_results.append(result)
    else:
        # Parallel execution
        with ThreadPoolExecutor(max_workers=len(subtasks)) as executor:
            futures = {}
            for st in subtasks:
                future = executor.submit(
                    _execute_subtask, st, messages, body, route_fn
                )
                futures[future] = st["intent"]

            for future in as_completed(futures):
                try:
                    result = future.result(timeout=120)
                    subtask_results.append(result)
                except Exception as e:
                    intent_name = futures[future]
                    logger.error("Agent Router: future %s failed: %s", intent_name, e)
                    subtask_results.append({
                        "intent": intent_name,
                        "model": "opsora-brain",
                        "content": f"[Error: {str(e)[:200]}]",
                        "provider": "error",
                        "real_model": "error",
                        "status": 500,
                    })

    elapsed = time.time() - t0

    # Synthesize results
    synthesized_content = _synthesize_results(subtask_results, messages)

    # Build agent metadata
    agent_metadata = {
        "router": "opsora-agent",
        "intent": intent,
        "subtask_count": len(subtasks),
        "models_used": list(set(r["model"] for r in subtask_results if r["status"] == 200)),
        "providers_used": list(set(r["provider"] for r in subtask_results if r["status"] == 200)),
        "latency_ms": round(elapsed * 1000, 1),
        "subtask_details": [
            {
                "intent": r["intent"],
                "model": r["model"],
                "real_model": r["real_model"],
                "status": r["status"],
            }
            for r in subtask_results
        ],
    }

    # Collect token usage from all subtasks
    total_input = 0
    total_output = 0
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    for result in subtask_results:
        if result["status"] == 200:
            # Try to get usage from the original result
            pass  # usage tracked at server level

    # Build OpenAI-compatible response with agent metadata
    response = {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "opsora-agent",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": synthesized_content,
                },
                "finish_reason": "stop",
            }
        ],
        "agent_metadata": agent_metadata,
    }

    # Determine overall status
    any_success = any(r["status"] == 200 for r in subtask_results)
    status = 200 if any_success else 502
    provider = "opsora-agent"
    real_model = "opsora-agent"

    logger.info(
        "Agent Router: %s intent=%s subtasks=%d models=%s elapsed=%.2fs",
        "OK" if status == 200 else "FAIL",
        intent, len(subtasks),
        agent_metadata["models_used"], elapsed,
    )

    return response, status, provider, real_model


# ---------------------------------------------------------------------------
# Streaming Router (simplified — single best model)
# ---------------------------------------------------------------------------


def route_agent_streaming(messages, body, route_streaming_fn):
    """Streaming variant — routes to single best model.

    Full multi-model synthesis is not possible with streaming, so we
    detect intent and route to the best single model.

    Args:
        messages: list of chat messages
        body: full request body dict
        route_streaming_fn: _route_with_fallback_streaming from opsora_server.py

    Returns:
        (http_response, status, provider, real_model, error_dict)
    """
    intent = detect_intent(messages)
    model = select_model(intent)
    logger.info("Agent Router [stream]: intent=%s → model=%s", intent, model)

    return route_streaming_fn(model, "/chat/completions", body)
