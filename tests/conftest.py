from pathlib import Path
import pytest
from workspace_bridge.cli import initialize
from workspace_bridge.service import Service

from runtime_fakes import FakeRuntime, RecordingNotifier

@pytest.fixture
def env(tmp_path):
    parent = tmp_path / "projects"; parent.mkdir()
    root = parent / "alpha"; root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "README.md").write_text("# Alpha\nSmall example project\n")
    state = tmp_path / "private-state"
    cfg = initialize(state, [str(parent)], 8765, 8766)
    service = Service(state, cfg)
    result = service.add_workspace("Alpha", str(root), [])
    ws_id, token = result["workspace"]["id"], service.manage_bridge("rotate_token")["token"]
    service.manage_workspace(ws_id, "enable")
    yield {"service": service, "root": root, "parent": parent, "state": state,
           "config": cfg, "id": ws_id, "token": token, "tmp": tmp_path}
    service.close()

@pytest.fixture
def agent_env(tmp_path):
    """A workspace with agent execution enabled, backed by a scripted runtime.

    The orchestrator runs in synchronous mode so tests can drive lifecycle
    transitions deterministically without threads.
    """
    parent = tmp_path / "projects"; parent.mkdir()
    root = parent / "alpha"; root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "README.md").write_text("# Alpha\nSmall example project\n")
    state = tmp_path / "private-state"
    cfg = initialize(state, [str(parent)], 8765, 8766)
    runtime = FakeRuntime(str(root))
    notifier = RecordingNotifier()
    service = Service(state, cfg, runtime=runtime, notifier=notifier,
                      orchestrator_background=False)
    result = service.add_workspace("Alpha", str(root), [])
    ws_id, token = result["workspace"]["id"], service.manage_bridge("rotate_token")["token"]
    service.manage_workspace(ws_id, "enable")
    service.manage_workspace(ws_id, "set_agent_enabled", agent_enabled=True)
    # New runs are fail-closed until the local administrator saves a global
    # model policy; this fixture mirrors a configured bridge.
    service.orchestrator.set_model_policy(
        ["anthropic/claude-sonnet", "glm/zai-glm-5.2"], "anthropic/claude-sonnet")
    yield {"service": service, "runtime": runtime, "notifier": notifier, "root": root,
           "parent": parent, "state": state, "config": cfg, "id": ws_id, "token": token,
           "tmp": tmp_path}
    service.close()

@pytest.fixture
def payload():
    return {"request_id": "example-1", "title": "Improve add", "goal": "Improve arithmetic behavior",
        "plan": "Inspect src/main.py; make the smallest necessary change and update tests.",
        "acceptance": "Run the unit tests. Preserve pre-existing user edits.",
        "constraints": "Do not commit or push.", "context": "Small Python module.", "context_hashes": {}}
