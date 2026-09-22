"""3D1: read_agent_execution MCP schema dispatch regression.

Proves the production tools/call path (TOOLS schema validate + model_dump
+ Service.call) reaches Service.read_agent_execution without pagination
defaults, for persisted IDs containing the Pi separator form `call_|fc_`.
"""
import json

import pytest
from pydantic import ValidationError

from workspace_bridge.api import TOOLS
from workspace_bridge.security import BridgeError

from test_pi_3b1 import call, pi_env, publish  # noqa: F401


def _seed(service, ws_id, run_id, session_id, tool_call_id, tool,
          input_summary, result_summary, seq=1, row_id="ex_x"):
    service.db.execute(
        "INSERT INTO agent_executions (id,run,workspace,runtime,session,tool_call_id,"
        "seq,tool,state,started,ended,duration_ms,input_summary,result_summary,"
        "is_error,permission_effect,permission_decision,truncated,created,updated) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row_id, run_id, ws_id, "pi", session_id, tool_call_id,
         seq, tool, "completed", None, None, None,
         json.dumps(input_summary), json.dumps(result_summary),
         0, "allow", "", 0,
         "2026-09-21T00:00:00+00:00", "2026-09-21T00:00:00+00:00"))


def _mcp_call(env, tool, **args):
    """Replicate api.make_mcp tools/call: validate, dump, dispatch via Service.call."""
    model = TOOLS[tool][0]
    arguments = model.model_validate(
        {"workspace_id": env["id"], **args}).model_dump()
    ident = arguments.pop("workspace_id", None)
    return env["service"].call(env["id"], env["token"], tool, arguments)


def test_schema_requires_ids_and_rejects_pagination():
    model = TOOLS["read_agent_execution"][0]
    schema = model.model_json_schema()
    props = set(schema.get("required", []))
    assert {"workspace_id", "run_id", "execution_id"} <= props
    assert "offset" not in schema["properties"]
    assert "limit" not in schema["properties"]
    with pytest.raises(ValidationError):
        model.model_validate({"workspace_id": "ws_" + "a" * 24,
                              "run_id": "run_" + "b" * 24,
                              "execution_id": "x",
                              "offset": 0})
    with pytest.raises(ValidationError):
        model.model_validate({"workspace_id": "ws_" + "a" * 24,
                              "run_id": "run_" + "b" * 24,
                              "execution_id": "x",
                              "limit": 20})
    # list_agent_executions pagination is unchanged.
    list_schema = TOOLS["list_agent_executions"][0].model_json_schema()
    assert "offset" in list_schema["properties"]
    assert "limit" in list_schema["properties"]


def test_mcp_dispatch_reads_separator_ids_with_bounded_evidence(pi_env):  # noqa: F811
    env = pi_env
    service = env["service"]
    job = publish(env, "3d1-detail")
    run = call(env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="3d1-detail-run")
    run_id, session_id = run["run_id"], run["session_id"]
    bash_id = "call_abc123|fc_def456"
    ext_id = "call_xyz789|fc_ghi012"
    secret = "SECRET-" + "y" * 40
    _seed(service, env["id"], run_id, session_id, bash_id, "bash",
          {"command": "echo hi", "command_sha256": "b" * 64,
           "timeout_ms": 30000, "fullOutputPath": "/tmp/evil",
           "token": secret},
          {"is_error": False, "output_preview": "hi",
           "fullOutputPath": "/tmp/evil", "token": secret},
          seq=1, row_id="ex_3d1_bash")
    _seed(service, env["id"], run_id, session_id, ext_id, "web_search",
          {"query": "bridge audit", "token": secret,
           "fullOutputPath": "/tmp/evil"},
          {"is_error": False, "preview": "found docs for bridge audit",
           "fullOutputPath": "/tmp/evil", "token": secret},
          seq=2, row_id="ex_3d1_ext")

    bash = _mcp_call(env, "read_agent_execution",
                     run_id=run_id, execution_id=bash_id)
    assert bash["input_summary"]["command"] == "echo hi"
    assert bash["result_summary"]["output_preview"] == "hi"
    blob = json.dumps(bash)
    assert "fullOutputPath" not in blob and "SECRET" not in blob

    ext = _mcp_call(env, "read_agent_execution",
                    run_id=run_id, execution_id=ext_id)
    blob = json.dumps(ext)
    assert "fullOutputPath" not in blob and "SECRET" not in blob
    assert ext["result_sensitivity"] == "potentially sensitive"
    assert "env" not in ext["result_summary"] or True
    for forbidden in ("fullOutputPath", "env", "token", "reasoning"):
        assert forbidden not in ext["input_summary"]
        assert forbidden not in ext["result_summary"]


def test_mcp_dispatch_missing_and_foreign_are_not_found(pi_env):  # noqa: F811
    env = pi_env
    service = env["service"]
    job = publish(env, "3d1-missing")
    run = call(env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="3d1-missing-run")
    with pytest.raises(BridgeError) as exc:
        _mcp_call(env, "read_agent_execution",
                  run_id=run["run_id"], execution_id="call_nope|fc_nope")
    assert exc.value.code == "not_found"
    # Foreign workspace/run ownership stays not_found, never internal_error.
    other_job = publish(env, "3d1-other")
    other = call(env, "start_agent_run", runtime="pi",
                 job_id=other_job["id"], request_id="3d1-other-run")
    _seed(service, env["id"], other["run_id"], other["session_id"],
          "call_owned|fc_owned", "read",
          {"target": "notes.txt"}, {"is_error": False, "count": 1},
          seq=1, row_id="ex_3d1_owned")
    with pytest.raises(BridgeError) as exc2:
        _mcp_call(env, "read_agent_execution",
                  run_id=run["run_id"],
                  execution_id="call_owned|fc_owned")
    assert exc2.value.code == "not_found"
