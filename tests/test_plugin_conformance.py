"""The plugin conformance kit against a scripted agent, clean and with one flaw each."""
import json
import sys
from pathlib import Path

import pytest

from workspace_bridge import cli
from workspace_bridge.plugin_conformance import run_kit
from workspace_bridge.plugin_protocol import validate_manifest

AGENT = Path(__file__).parent / "plugins" / "conformance_agent.py"
ENFORCES = ["read_inside", "read_outside", "write_inside", "write_outside", "shell", "web",
            "extensions", "protected", "external_roots", "commands"]
MANIFEST = validate_manifest({
    "id": "conformance-agent", "version": "1", "protocol": 2, "runtime_type": "pi",
    "command": [sys.executable, str(AGENT)], "settings": ["flaw"], "enforces": ENFORCES})


def _statuses(report):
    return {check["check"]: check["status"] for check in report["checks"]}


def test_a_conforming_agent_passes_every_check():
    report = run_kit(MANIFEST, live=True, timeout=20)
    assert report["passed"] is True
    assert set(_statuses(report).values()) == {"pass"}
    assert list(_statuses(report)) == [
        "descriptor", "unknown_method", "models", "security_refused", "projects_root",
        "conversation", "ownership", "run", "unknown_model", "interaction", "events",
        "restart", "continuation"]


def test_without_live_only_protocol_checks_run():
    report = run_kit(MANIFEST, timeout=20)
    statuses = _statuses(report)
    assert report["passed"] is True and report["live"] is False
    assert statuses["conversation"] == "pass" and statuses["run"] == "skip"


@pytest.mark.parametrize(("flaw", "check"), [
    ("busy_start", "interaction"),
    ("adopt", "ownership"),
    ("replay", "restart"),
    ("echo", "conversation"),
    ("events", "events"),
    ("unknown_model", "unknown_model"),
    ("unasked", "interaction"),
])
def test_each_broken_rule_fails_its_check(flaw, check):
    report = run_kit(MANIFEST, settings={"flaw": flaw}, live=True, timeout=20)
    assert report["passed"] is False
    assert _statuses(report)[check] == "fail"


def test_the_command_exits_nonzero_unless_the_plugin_passes(tmp_path, capsys):
    manifest = tmp_path / "agent.toml"
    manifest.write_text(
        'id = "conformance-agent"\nversion = "1"\nprotocol = 2\nruntime_type = "pi"\n'
        f"command = {json.dumps([sys.executable, str(AGENT)])}\n"
        'settings = ["flaw"]\n' f"enforces = {json.dumps(ENFORCES)}\n")
    cli.main(["plugin", "test", str(manifest), "--timeout", "20"])
    assert "PASSED: conformance-agent 1 (protocol checks only" in capsys.readouterr().out
    with pytest.raises(SystemExit) as failed:
        cli.main(["plugin", "test", str(manifest), "--set", "flaw=adopt", "--timeout", "20"])
    assert failed.value.code == 1
    assert "[fail] ownership" in capsys.readouterr().out
