#!/usr/bin/env python3
"""Opsora Agent Loop — ReAct (Reasoning + Acting) agent loop.

Stdlib-only. Implements the Think → Act → Observe loop:
1. Send messages + tools to LLM via existing _route_with_fallback
2. Parse tool_calls from response
3. Execute tools, return results as tool messages
4. Repeat until LLM produces final answer (no tool_calls)
5. Max iterations configurable (default 10)

Architecture based on Anthropic's "Building Effective Agents" best practices:
- Environment feedback at every step (tool results)
- Error handling returns error text, not exceptions
- Transparent planning steps visible in metadata
- Checkpoint-ready state management

Zero external dependencies — Python stdlib only.
"""

import json
import logging
import time
import uuid

from tools import execute_tool, get_tool_schemas, get_tool_names

logger = logging.getLogger("opsora.agent_loop")

# ---------------------------------------------------------------------------
# Agent Loop Configuration
# ---------------------------------------------------------------------------

DEFAULT_MAX_ITERATIONS = 10
DEFAULT_WORKSPACE = "/app/workspace"

SYSTEM_PROMPT = """You are Opsora Agent, an AI coding assistant with access to tools for file operations, code search, shell commands, and web access.

## Capabilities
- Read, write, and edit files in the workspace
- Search code with grep patterns and glob file matching
- Execute shell commands (builds, tests, git, system commands)
- Fetch web content (documentation, APIs, references)
- List directory contents

## Guidelines
- Use tools proactively to investigate before responding
- Read files before editing to understand context
- Search the codebase for patterns before implementing
- Run tests after making code changes
- Be direct, precise, and concise in responses
- Show file paths when referencing files
- If a tool returns an error, read the error and adjust your approach
- For multi-step tasks, explain your plan before executing"""


# ---------------------------------------------------------------------------
# Agent Loop
# ---------------------------------------------------------------------------

class AgentLoop:
    """ReAct agent: Think → Act → Observe loop with tool execution.

    The agent loop sends messages + tool definitions to an LLM,
    executes any tool calls the LLM requests, feeds results back,
    and repeats until the LLM produces a final text response.
    """

    def __init__(self, model_alias="opsora-agent", workspace=None, max_iterations=None):
        self.model = model_alias
        self.workspace = workspace or DEFAULT_WORKSPACE
        self.max_iterations = max_iterations or DEFAULT_MAX_ITERATIONS
        self.iterations = 0
        self.tools_used = []
        self.total_latency = 0
        self.tool_call_count = 0

    def run(self, messages, route_fn, system_prompt=None):
        """Execute the agent loop.

        Args:
            messages: List of chat messages (OpenAI format)
            route_fn: Function to call LLM — signature:
                      route_fn(model_alias, endpoint, body) -> (result, status, provider, real_model)
            system_prompt: Optional override for system prompt

        Returns:
            dict with:
                - content: Final text response from the LLM
                - metadata: Agent execution metadata
                - status: HTTP status code (200 on success)
                - provider: Provider used for final response
                - model: Actual model used
        """
        t_start = time.time()
        sys_prompt = system_prompt or SYSTEM_PROMPT
        tools = get_tool_schemas()

        # Build conversation history
        history = [{"role": "system", "content": sys_prompt}] + list(messages)

        last_content = ""
        last_provider = ""
        last_model = ""
        last_status = 200

        for iteration in range(self.max_iterations):
            self.iterations = iteration + 1
            t_iter = time.time()

            # Build request body
            body = {
                "messages": history,
                "tools": tools,
                "tool_choice": "auto",
                "stream": False,
            }

            # Call LLM
            try:
                result, status, provider, real_model = route_fn(
                    self.model, "/chat/completions", body
                )
            except Exception as e:
                logger.error("Agent loop: LLM call failed at iteration %d: %s", iteration, e)
                return self._build_error_response(
                    f"LLM call failed: {type(e).__name__}: {str(e)[:300]}",
                    t_start,
                )

            if status != 200:
                logger.warning("Agent loop: LLM returned status %d at iteration %d", status, iteration)
                return self._build_error_response(
                    f"LLM returned error status {status}",
                    t_start,
                )

            last_provider = provider
            last_model = real_model

            # Parse response
            if not isinstance(result, dict):
                return self._build_error_response(
                    "LLM returned non-dict response",
                    t_start,
                )

            choices = result.get("choices", [])
            if not choices:
                return self._build_error_response(
                    "LLM response has no choices",
                    t_start,
                )

            message = choices[0].get("message", {})
            content = message.get("content", "") or ""
            tool_calls = message.get("tool_calls")

            # Add assistant message to history
            assistant_msg = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            history.append(assistant_msg)

            # No tool calls → final response
            if not tool_calls:
                last_content = content
                break

            # Execute tool calls
            tool_results = []
            for tc in tool_calls:
                func = tc.get("function", {})
                tc_name = func.get("name", "unknown")
                tc_id = tc.get("id", f"call_{uuid.uuid4().hex[:12]}")

                # Parse arguments
                try:
                    tc_args = json.loads(func.get("arguments", "{}")) if isinstance(func.get("arguments"), str) else func.get("arguments", {})
                except json.JSONDecodeError:
                    tc_args = {}

                # Log tool usage
                self.tool_call_count += 1
                if tc_name not in self.tools_used:
                    self.tools_used.append(tc_name)

                logger.info(
                    "Agent loop [%d/%d]: tool=%s args=%s",
                    iteration + 1,
                    self.max_iterations,
                    tc_name,
                    json.dumps(tc_args, ensure_ascii=False)[:200],
                )

                # Execute tool
                output = execute_tool(tc_name, tc_args, self.workspace)

                logger.info(
                    "Agent loop [%d/%d]: %s → %d chars output",
                    iteration + 1,
                    self.max_iterations,
                    tc_name,
                    len(output),
                )

                # Add tool result to history
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": output,
                })

            # Append all tool results to history
            history.extend(tool_results)

            # Check for infinite loop (same tool called 3+ times with same args)
            if self._detect_loop(history):
                logger.warning("Agent loop: detected potential infinite loop at iteration %d", iteration)
                # Add a nudge message
                history.append({
                    "role": "user",
                    "content": "You seem to be repeating the same action. Please try a different approach or provide your final answer based on what you've learned so far.",
                })

        else:
            # Max iterations reached without final response
            last_content = f"I've reached the maximum number of steps ({self.max_iterations}). Here's what I found so far based on my investigation."
            if content:
                last_content += f"\n\n{content}"

        self.total_latency = time.time() - t_start

        metadata = {
            "iterations": self.iterations,
            "tool_calls": self.tool_call_count,
            "tools_used": self.tools_used,
            "total_latency_ms": round(self.total_latency * 1000, 1),
            "model": self.model,
            "real_model": last_model,
            "provider": last_provider,
            "workspace": self.workspace,
            "max_iterations": self.max_iterations,
        }

        return {
            "content": last_content,
            "metadata": metadata,
            "status": 200,
            "provider": last_provider,
            "model": last_model,
        }

    def run_streaming(self, messages, route_fn, system_prompt=None):
        """Execute agent loop with streaming SSE events.

        Yields SSE-compatible dicts that can be serialized and sent as
        Server-Sent Events to the client.

        Event types:
        - {"type": "thinking", "iteration": N}
        - {"type": "tool_use", "name": "...", "args": {...}}
        - {"type": "tool_result", "name": "...", "output": "..."}
        - {"type": "content", "delta": "..."}
        - {"type": "done", "metadata": {...}}
        """
        t_start = time.time()
        sys_prompt = system_prompt or SYSTEM_PROMPT
        tools = get_tool_schemas()
        history = [{"role": "system", "content": sys_prompt}] + list(messages)

        for iteration in range(self.max_iterations):
            self.iterations = iteration + 1

            yield {"type": "thinking", "iteration": iteration + 1}

            body = {
                "messages": history,
                "tools": tools,
                "tool_choice": "auto",
                "stream": False,
            }

            try:
                result, status, provider, real_model = route_fn(
                    self.model, "/chat/completions", body
                )
            except Exception as e:
                yield {"type": "error", "message": str(e)[:500]}
                return

            if status != 200 or not isinstance(result, dict):
                yield {"type": "error", "message": f"LLM error: status {status}"}
                return

            choices = result.get("choices", [])
            if not choices:
                yield {"type": "error", "message": "No choices in response"}
                return

            message = choices[0].get("message", {})
            content = message.get("content", "") or ""
            tool_calls = message.get("tool_calls")

            assistant_msg = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            history.append(assistant_msg)

            if not tool_calls:
                # Stream final content
                if content:
                    yield {"type": "content", "delta": content}
                break

            # Execute tools and stream results
            for tc in tool_calls:
                func = tc.get("function", {})
                tc_name = func.get("name", "unknown")
                tc_id = tc.get("id", f"call_{uuid.uuid4().hex[:12]}")

                try:
                    tc_args = json.loads(func.get("arguments", "{}")) if isinstance(func.get("arguments"), str) else func.get("arguments", {})
                except json.JSONDecodeError:
                    tc_args = {}

                self.tool_call_count += 1
                if tc_name not in self.tools_used:
                    self.tools_used.append(tc_name)

                yield {"type": "tool_use", "name": tc_name, "args": tc_args}

                output = execute_tool(tc_name, tc_args, self.workspace)

                yield {
                    "type": "tool_result",
                    "name": tc_name,
                    "output": output[:2000],  # Truncate for streaming
                    "full_length": len(output),
                }

                history.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": output,
                })

            if self._detect_loop(history):
                history.append({
                    "role": "user",
                    "content": "You seem to be repeating the same action. Please try a different approach or provide your final answer.",
                })

        self.total_latency = time.time() - t_start

        yield {
            "type": "done",
            "metadata": {
                "iterations": self.iterations,
                "tool_calls": self.tool_call_count,
                "tools_used": self.tools_used,
                "total_latency_ms": round(self.total_latency * 1000, 1),
            },
        }

    def _detect_loop(self, history, threshold=3):
        """Detect if the agent is stuck in a repetitive loop.

        Checks if the same tool with the same arguments was called
        `threshold` times consecutively.
        """
        tool_msgs = [m for m in history if m.get("role") == "tool"]
        if len(tool_msgs) < threshold:
            return False

        # Check last N tool messages for repetition
        recent = tool_msgs[-threshold:]
        first_content = recent[0].get("content", "")[:200]
        return all(m.get("content", "")[:200] == first_content for m in recent)

    def _build_error_response(self, error_msg, t_start):
        """Build an error response dict."""
        return {
            "content": f"Agent error: {error_msg}",
            "metadata": {
                "iterations": self.iterations,
                "tool_calls": self.tool_call_count,
                "tools_used": self.tools_used,
                "total_latency_ms": round((time.time() - t_start) * 1000, 1),
                "error": error_msg,
            },
            "status": 500,
            "provider": "error",
            "model": "error",
        }
