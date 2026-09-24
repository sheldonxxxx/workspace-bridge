from pathlib import Path
import pytest
from workspace_bridge.cli import initialize
from workspace_bridge.service import Service

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
def payload():
    return {"request_id": "example-1", "title": "Improve add", "goal": "Improve arithmetic behavior",
        "plan": "Inspect src/main.py; make the smallest necessary change and update tests.",
        "acceptance": "Run the unit tests. Preserve pre-existing user edits.",
        "constraints": "Do not commit or push.", "context": "Small Python module.", "context_hashes": {}}
