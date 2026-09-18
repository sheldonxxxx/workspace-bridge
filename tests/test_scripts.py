import importlib.util
from pathlib import Path
import pytest


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_url_is_loopback_only():
    smoke = load("smoke_mcp")
    assert smoke.validate_url("http://127.0.0.1:8765/mcp")
    for url in ["https://example.com/mcp", "http://localhost:8765/api/workspaces",
                "http://secret@localhost:8765/mcp", "http://localhost:8765/mcp?token=x", "http://localhost:8765/mcp/ws_123"]:
        with pytest.raises(ValueError):
            smoke.validate_url(url)


def test_tunnel_commands_are_explicit_and_no_shell():
    launcher = load("run_tunnel")
    assert launcher.build_command("/bin/tunnel-client", Path("/tmp/local.yaml"), "doctor") == [
        "/bin/tunnel-client", "doctor", "--config", "/tmp/local.yaml", "--explain"]
    with pytest.raises(ValueError):
        launcher.build_command("/bin/tunnel-client", Path("/tmp/local.yaml"), "quickstart")


def test_smoke_never_follows_redirects():
    assert load("smoke_mcp").NoRedirect().redirect_request(None, None, 302, "redirect", {}, "http://example.com") is None
