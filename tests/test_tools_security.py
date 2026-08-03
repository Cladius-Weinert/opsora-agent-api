"""Security tests for tools.py: path traversal validation and the
run_command guard, plus the server's open-dev-mode startup guard."""

import os
import subprocess
import sys

import pytest

import tools

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "hello.txt").write_text("hello world", encoding="utf-8")
    return str(ws)


# ---------------------------------------------------------------------------
# _validate_path traversal cases
# ---------------------------------------------------------------------------

class TestValidatePath:
    def test_relative_path_inside_workspace_ok(self, workspace):
        resolved, err = tools._validate_path("hello.txt", workspace)
        assert err is None
        assert resolved.exists()

    def test_nested_relative_path_ok(self, workspace):
        resolved, err = tools._validate_path("sub/dir/file.txt", workspace)
        assert err is None
        assert str(resolved).startswith(str(workspace))

    def test_dotdot_traversal_blocked(self, workspace):
        resolved, err = tools._validate_path("../outside.txt", workspace)
        assert resolved is None
        assert "outside the workspace" in err

    def test_deep_dotdot_traversal_blocked(self, workspace):
        resolved, err = tools._validate_path("a/../../../../../etc/passwd", workspace)
        assert resolved is None
        assert "outside the workspace" in err

    def test_absolute_path_outside_workspace_blocked(self, workspace):
        resolved, err = tools._validate_path("/etc/passwd", workspace)
        assert resolved is None
        assert "outside the workspace" in err

    def test_sibling_dir_with_same_prefix_blocked(self, workspace):
        # "/.../workspace-evil" must NOT pass just because it startswith
        # the workspace string.
        evil = workspace + "-evil"
        resolved, err = tools._validate_path(os.path.join(evil, "f.txt"), workspace)
        assert resolved is None
        assert "outside the workspace" in err

    def test_blocked_credential_dir(self, workspace):
        resolved, err = tools._validate_path(".ssh/id_rsa", workspace)
        assert resolved is None
        assert "credential directories" in err

    def test_blocked_env_file(self, workspace):
        resolved, err = tools._validate_path(".env", workspace)
        assert resolved is None
        assert "sensitive files" in err

    def test_blocked_env_file_nested(self, workspace):
        resolved, err = tools._validate_path("config/.env.production", workspace)
        assert resolved is None
        assert "sensitive files" in err

    def test_read_file_tool_respects_traversal_guard(self, workspace):
        out = tools.execute_tool("read_file", {"path": "../../etc/passwd"}, workspace)
        assert out.startswith("ERROR")

    def test_write_file_tool_cannot_escape_workspace(self, workspace, tmp_path):
        out = tools.execute_tool(
            "write_file",
            {"path": "../escaped.txt", "content": "pwned"},
            workspace,
        )
        assert out.startswith("ERROR")
        assert not (tmp_path / "escaped.txt").exists()


# ---------------------------------------------------------------------------
# run_command guard matrix
# ---------------------------------------------------------------------------

class TestRunCommandGuard:
    def test_disabled_by_default_in_open_dev_mode(self, monkeypatch, workspace):
        monkeypatch.delenv("OPSORA_DISABLE_RUN_COMMAND", raising=False)
        monkeypatch.setenv("OPSORA_API_KEYS", "")
        out = tools.execute_tool("run_command", {"command": "echo pwned"}, workspace)
        assert out.startswith("ERROR: run_command is disabled")
        assert "pwned" not in out

    def test_enabled_when_api_keys_configured(self, monkeypatch, workspace):
        monkeypatch.delenv("OPSORA_DISABLE_RUN_COMMAND", raising=False)
        monkeypatch.setenv("OPSORA_API_KEYS", "opsk-some-client-key")
        out = tools.execute_tool("run_command", {"command": "echo hello"}, workspace)
        assert "hello" in out

    def test_explicit_disable_wins_over_keys(self, monkeypatch, workspace):
        monkeypatch.setenv("OPSORA_API_KEYS", "opsk-some-client-key")
        monkeypatch.setenv("OPSORA_DISABLE_RUN_COMMAND", "1")
        out = tools.execute_tool("run_command", {"command": "echo hi"}, workspace)
        assert out.startswith("ERROR: run_command is disabled")

    def test_explicit_enable_in_open_mode(self, monkeypatch, workspace):
        monkeypatch.setenv("OPSORA_API_KEYS", "")
        monkeypatch.setenv("OPSORA_DISABLE_RUN_COMMAND", "0")
        out = tools.execute_tool("run_command", {"command": "echo optin"}, workspace)
        assert "optin" in out

    def test_flag_truthy_variants(self, monkeypatch):
        monkeypatch.setenv("OPSORA_API_KEYS", "opsk-x")
        for v in ("1", "true", "YES", "on"):
            monkeypatch.setenv("OPSORA_DISABLE_RUN_COMMAND", v)
            assert tools.run_command_enabled() is False

    def test_flag_falsey_variants(self, monkeypatch):
        monkeypatch.setenv("OPSORA_API_KEYS", "")
        for v in ("0", "false", "NO", "off"):
            monkeypatch.setenv("OPSORA_DISABLE_RUN_COMMAND", v)
            assert tools.run_command_enabled() is True


# ---------------------------------------------------------------------------
# Server startup guard (subprocess — runs the real opsora_server.py)
# ---------------------------------------------------------------------------

class TestServerStartupGuard:
    def _server_env(self, tmp_path, extra_env, port):
        env = {
            "PATH": os.environ.get("PATH", ""),
            "NVIDIA_API_KEY": "nvapi-test-dummy",
            "OPSORA_API_KEYS": "",
            "DB_PATH": str(tmp_path / "usage.db"),
            # Point the billing DB at an impossible path so BillingEngine
            # init fails -> no auth mechanism at all -> guard must trigger.
            "BILLING_DB_PATH": "/nonexistent-dir-xyz/billing.db",
            "PORT": str(port),
        }
        env.update(extra_env)
        return env

    def _spawn(self, tmp_path, extra_env, port):
        env = self._server_env(tmp_path, extra_env, port)
        return subprocess.Popen(
            [sys.executable, os.path.join(_REPO_ROOT, "opsora_server.py")],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def _wait_healthy(self, proc, port, timeout_s=10):
        import time
        from urllib.request import urlopen
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if proc.poll() is not None:
                return False  # exited early
            try:
                with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                    if r.status == 200:
                        return True
            except Exception:
                time.sleep(0.2)
        return False

    def test_refuses_to_start_open_without_opt_in(self, tmp_path):
        env = self._server_env(tmp_path, {}, 18199)
        r = subprocess.run(
            [sys.executable, os.path.join(_REPO_ROOT, "opsora_server.py")],
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 1
        assert "Refusing to start" in r.stderr

    def test_starts_when_unauthenticated_explicitly_allowed(self, tmp_path):
        proc = self._spawn(tmp_path, {"OPSORA_ALLOW_UNAUTHENTICATED": "1"}, 18200)
        try:
            assert self._wait_healthy(proc, 18200), \
                f"server exited early rc={proc.poll()}"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_starts_when_api_keys_configured(self, tmp_path):
        proc = self._spawn(tmp_path, {"OPSORA_API_KEYS": "opsk-test-key-123"}, 18201)
        try:
            assert self._wait_healthy(proc, 18201), \
                f"server exited early rc={proc.poll()}"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
