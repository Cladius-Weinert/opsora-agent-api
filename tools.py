#!/usr/bin/env python3
"""Opsora Agent Tools — Server-side tool execution for the agent loop.

Stdlib-only. All tools use Python standard library (subprocess, pathlib, urllib).
Tool schemas follow OpenAI function calling format.

Security:
- All file operations scoped to workspace directory
- Shell commands have timeout (30s default)
- Output truncated to 50KB
- Blocked paths: .ssh, .aws, .gnupg, credentials files
"""

import json
import os
import re
import subprocess
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

# Maximum output size per tool call (50KB)
MAX_OUTPUT = 50_000

# Blocked directory names (case-insensitive)
BLOCKED_DIRS = {".ssh", ".aws", ".gnupg", ".config/gcloud", "credentials"}

# Blocked file patterns
BLOCKED_FILES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}

# Default workspace (overridable per request)
DEFAULT_WORKSPACE = os.getenv("AGENT_WORKSPACE", "/app/workspace")


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

def run_command_enabled():
    """Decide whether the run_command (shell) tool may execute.

    Controlled by OPSORA_DISABLE_RUN_COMMAND:
      - "1"/"true"/"yes"/"on"   -> always disabled
      - "0"/"false"/"no"/"off"  -> always enabled (explicit opt-in)
      - unset (default)          -> disabled when no client API keys are
        configured (OPSORA_API_KEYS empty), i.e. open dev mode. In that mode
        the tool is an unauthenticated remote shell and must stay off unless
        explicitly enabled.
    """
    flag = os.getenv("OPSORA_DISABLE_RUN_COMMAND", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return False
    if flag in ("0", "false", "no", "off"):
        return True
    return bool(os.getenv("OPSORA_API_KEYS", "").strip())


def _validate_path(filepath, workspace):
    """Validate that a file path is within the workspace and not blocked."""
    p = Path(filepath)
    if not p.is_absolute():
        p = Path(workspace) / p

    resolved = p.resolve()
    ws = Path(workspace).resolve()

    # is_relative_to (not str.startswith) — startswith would let
    # "/app/workspace-evil" pass for workspace "/app/workspace".
    if not resolved.is_relative_to(ws):
        return None, "ERROR: Path is outside the workspace directory."

    parts_lower = {part.casefold() for part in resolved.parts}
    if parts_lower & BLOCKED_DIRS:
        return None, f"ERROR: Access to credential directories is blocked."

    if resolved.name.casefold() in {b.casefold() for b in BLOCKED_FILES}:
        return None, f"ERROR: Access to sensitive files ({resolved.name}) is blocked."

    return resolved, None


def _truncate(text, max_chars=MAX_OUTPUT):
    """Truncate output to max_chars with indicator."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n… [truncated, {len(text)} total chars]"


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function calling format)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a text file from the workspace. Returns file content as string. Supports offset and limit for reading specific line ranges of large files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to workspace root"},
                    "offset": {"type": "integer", "description": "Line number to start reading from (0-based, default 0)"},
                    "limit": {"type": "integer", "description": "Maximum number of lines to read (default: entire file)"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file in the workspace. Parent directories are created automatically. Use for creating new files or completely replacing file contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to workspace root"},
                    "content": {"type": "string", "description": "The full text content to write to the file"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Edit a file by finding and replacing an exact text match. Only replaces the first occurrence. Use for surgical code modifications — provide the exact old text and the new text to replace it with.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to workspace root"},
                    "old_string": {"type": "string", "description": "Exact text to find (must match precisely including whitespace and indentation)"},
                    "new_string": {"type": "string", "description": "Text to replace it with"},
                },
                "required": ["path", "old_string", "new_string"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": "Search file contents using regex patterns. Returns matching lines with file paths and line numbers. Use for finding code patterns, function definitions, error messages, or any text across the codebase.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "Directory or file to search in (default: workspace root)"},
                    "file_type": {"type": "string", "description": "File extension filter, e.g. 'py', 'js', 'ts' (default: all files)"},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_search",
            "description": "Find files matching a glob pattern. Returns a list of file paths. Use for discovering project structure, finding specific file types, or locating configuration files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern (e.g. '**/*.py', 'src/**/*.ts', 'package.json')"},
                    "base": {"type": "string", "description": "Base directory to search from (default: workspace root)"},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and subdirectories in a directory path. Returns names sorted alphabetically with type indicators ([DIR] for directories).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path relative to workspace root (default: workspace root)"},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a shell command in the workspace directory. Returns stdout and stderr combined. Commands have a 30-second timeout. Use for builds, tests, git operations, and system commands.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute (bash -c format)"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default: 30, max: 120)"},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch content from a URL. Returns page text with HTML tags stripped. Use for reading documentation, checking APIs, or retrieving web resources. Supports HTTP and HTTPS only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch (must start with http:// or https://)"},
                    "max_chars": {"type": "integer", "description": "Maximum characters to return (default: 50000)"},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def execute_tool(name, args, workspace=None):
    """Execute a tool by name with the given arguments.

    Args:
        name: Tool name (must match TOOL_SCHEMAS)
        args: Dict of arguments matching the tool's parameter schema
        workspace: Workspace directory path (default: DEFAULT_WORKSPACE)

    Returns:
        String output of the tool execution (success or error message)
    """
    ws = workspace or DEFAULT_WORKSPACE

    try:
        if name == "read_file":
            return _exec_read_file(args, ws)
        elif name == "write_file":
            return _exec_write_file(args, ws)
        elif name == "edit_file":
            return _exec_edit_file(args, ws)
        elif name == "grep_search":
            return _exec_grep_search(args, ws)
        elif name == "glob_search":
            return _exec_glob_search(args, ws)
        elif name == "list_directory":
            return _exec_list_directory(args, ws)
        elif name == "run_command":
            return _exec_run_command(args, ws)
        elif name == "web_fetch":
            return _exec_web_fetch(args)
        else:
            return f"ERROR: Unknown tool '{name}'. Available tools: {', '.join(t['function']['name'] for t in TOOL_SCHEMAS)}"
    except Exception as e:
        return f"Tool error ({name}): {type(e).__name__}: {str(e)[:500]}"


def _exec_read_file(args, workspace):
    resolved, err = _validate_path(args["path"], workspace)
    if err:
        return err
    if not resolved.exists():
        return f"ERROR: File not found: {args['path']}"
    if not resolved.is_file():
        return f"ERROR: Not a file: {args['path']}"

    offset = int(args.get("offset", 0))
    limit = args.get("limit")

    with open(resolved, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    total = len(lines)
    selected = lines[offset:]
    if limit:
        selected = selected[:int(limit)]

    result = "".join(selected)
    header = f"[{args['path']}] lines {offset+1}-{offset+len(selected)} of {total}\n" if offset or limit else ""
    return _truncate(header + result)


def _exec_write_file(args, workspace):
    resolved, err = _validate_path(args["path"], workspace)
    if err:
        return err

    resolved.parent.mkdir(parents=True, exist_ok=True)
    content = args["content"]
    resolved.write_text(content, encoding="utf-8")
    return f"✓ Wrote {len(content)} chars to {args['path']}"


def _exec_edit_file(args, workspace):
    resolved, err = _validate_path(args["path"], workspace)
    if err:
        return err
    if not resolved.exists():
        return f"ERROR: File not found: {args['path']}"

    content = resolved.read_text(encoding="utf-8")
    old_str = args["old_string"]
    new_str = args["new_string"]

    if old_str not in content:
        return f"ERROR: old_string not found in {args['path']}. Verify exact whitespace and indentation."

    count = content.count(old_str)
    new_content = content.replace(old_str, new_str, 1)
    resolved.write_text(new_content, encoding="utf-8")
    note = f" (note: {count} occurrences found, replaced first only)" if count > 1 else ""
    return f"✓ Edited {args['path']}: replaced 1 occurrence ({len(old_str)} → {len(new_str)} chars){note}"


def _exec_grep_search(args, workspace):
    pattern = args["pattern"]
    search_path = args.get("path", ".")

    resolved_search, err = _validate_path(search_path, workspace)
    if err:
        return err

    cmd = ["grep", "-rn", "--color=never"]
    file_type = args.get("file_type", "")
    if file_type:
        cmd.extend([f"--include=*.{file_type}"])
    cmd.extend([pattern, str(resolved_search)])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    output = result.stdout
    if not output:
        return f"No matches found for '{pattern}' in {search_path}"

    lines = output.strip().split("\n")
    if len(lines) > 200:
        output = "\n".join(lines[:200]) + f"\n… truncated ({len(lines)} total matches)"
    return _truncate(output)


def _exec_glob_search(args, workspace):
    import glob as glob_mod

    base = args.get("base", ".")
    resolved_base, err = _validate_path(base, workspace)
    if err:
        return err

    full_pattern = os.path.join(str(resolved_base), args["pattern"])
    matches = sorted(glob_mod.glob(full_pattern, recursive=True))[:100]
    files = [
        str(Path(m).relative_to(Path(workspace).resolve()))
        for m in matches
        if os.path.isfile(m)
    ]

    if not files:
        return f"No files matching '{args['pattern']}' in {base}"
    return json.dumps(files, indent=2)


def _exec_list_directory(args, workspace):
    path = args.get("path", ".")
    resolved, err = _validate_path(path, workspace)
    if err:
        return err
    if not resolved.exists():
        return f"ERROR: Directory not found: {path}"
    if not resolved.is_dir():
        return f"ERROR: Not a directory: {path}"

    entries = []
    for item in sorted(resolved.iterdir()):
        prefix = "[DIR] " if item.is_dir() else ""
        entries.append(f"{prefix}{item.name}")

    return _truncate("\n".join(entries) if entries else "(empty directory)")


def _exec_run_command(args, workspace):
    if not run_command_enabled():
        return (
            "ERROR: run_command is disabled. Shell execution is off by default "
            "when no API keys are configured (open dev mode). Set client API "
            "keys via OPSORA_API_KEYS, or explicitly opt in with "
            "OPSORA_DISABLE_RUN_COMMAND=0."
        )

    command = args["command"]
    timeout = min(int(args.get("timeout", 30)), 120)

    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=workspace,
    )
    output = (result.stdout or "") + (result.stderr or "")
    if not output.strip():
        output = f"(exit code {result.returncode}, no output)"
    return _truncate(output)


def _exec_web_fetch(args):
    url = args["url"]
    if not url.startswith(("http://", "https://")):
        return "ERROR: URL must start with http:// or https://"

    max_chars = int(args.get("max_chars", 50000))
    req = Request(url, headers={"User-Agent": "Opsora/2.0 Agent"})

    try:
        with urlopen(req, timeout=15) as resp:
            raw = resp.read(max_chars * 2).decode("utf-8", errors="replace")
    except (URLError, OSError) as e:
        return f"ERROR: Failed to fetch {url}: {type(e).__name__}: {e}"

    clean = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL)
    clean = re.sub(r"<style[^>]*>.*?</style>", "", clean, flags=re.DOTALL)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return _truncate(clean, max_chars)


# ---------------------------------------------------------------------------
# Utility: get tool schemas as list
# ---------------------------------------------------------------------------

def get_tool_schemas():
    """Return all tool schemas in OpenAI function calling format."""
    return TOOL_SCHEMAS


def get_tool_names():
    """Return list of available tool names."""
    return [t["function"]["name"] for t in TOOL_SCHEMAS]
