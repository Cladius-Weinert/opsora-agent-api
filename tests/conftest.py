"""Shared pytest setup for the Opsora Agent API test suite.

Makes the repo root importable and satisfies opsora_server.py's import-time
environment requirements with dummy values. NO test makes real API calls —
all provider access is mocked/monkeypatched.
"""

import os
import sys
import tempfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# opsora_server.py raises RuntimeError at import time without NVIDIA_API_KEY.
# A dummy key is fine: every provider call is mocked in the tests.
os.environ.setdefault("NVIDIA_API_KEY", "nvapi-test-dummy-key")
os.environ.setdefault(
    "DB_PATH",
    os.path.join(tempfile.mkdtemp(prefix="opsora-tests-"), "usage.db"),
)
# Ensure no Midtrans key leaks in from the environment during tests.
os.environ.pop("MIDTRANS_SERVER_KEY", None)
