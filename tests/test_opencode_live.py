"""Opt-in live smoke test against a real host OpenCode runtime.

Skipped unless WB_LIVE_OPENCODE=1 and WB_OPENCODE_RUNTIME_URL are supplied. This is
never required for CI and never touches provider credentials directly.
"""
import os

import pytest

from workspace_bridge.runtime import runtime_from_environment


pytestmark = pytest.mark.skipif(
    os.environ.get("WB_LIVE_OPENCODE") != "1" or not os.environ.get("WB_OPENCODE_RUNTIME_URL"),
    reason="live OpenCode smoke is opt-in; set WB_LIVE_OPENCODE=1 and WB_OPENCODE_RUNTIME_URL")


def test_live_runtime_health_and_models():
    runtime = runtime_from_environment()
    assert runtime is not None
    health = runtime.health()
    assert health["adapter_version"]
    directory = os.environ.get("WB_LIVE_OPENCODE_DIRECTORY")
    if directory:
        models = runtime.list_models(directory)
        assert all(m.selector and "/" in m.selector for m in models)
