"""Bridge security profiles: the shared rule vectors and the profile registry."""
import json
import os
from pathlib import Path

import pytest

from conftest import create_test_adapter
from workspace_bridge import security_profiles as sp
from workspace_bridge.profile_registry import adapter_security
from workspace_bridge.security import BridgeError

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "profile_vectors.json").read_text())


@pytest.fixture
def layout(tmp_path):
    for directory in VECTORS["layout"]["dirs"]:
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    for link, target in VECTORS["layout"]["symlinks"].items():
        os.symlink(target, tmp_path / link)
    return {"ws": str(tmp_path / "ws"), "out": os.path.realpath(tmp_path / "out")}


def _fill(value, out: str):
    return json.loads(json.dumps(value).replace("/@out", out))


@pytest.mark.parametrize("case", VECTORS["canonical"], ids=lambda case: case["revision"])
def test_vector_canonical_rules_and_revision(case):
    rules = sp.validate(case["raw"])
    assert rules == case["rules"]
    assert sp.revision(rules) == case["revision"]


@pytest.mark.parametrize("raw", VECTORS["invalid"])
def test_vector_invalid_rules_are_refused(raw):
    with pytest.raises(BridgeError) as caught:
        sp.validate(raw)
    assert caught.value.code == "invalid_profile"


@pytest.mark.parametrize("case", VECTORS["globs"], ids=lambda case: f"{case['pattern']}:{case['path']}")
def test_vector_globs(case):
    assert sp.glob_matches(case["pattern"], case["path"]) is case["match"]


@pytest.mark.parametrize("case", VECTORS["fixed"], ids=lambda case: case["path"])
def test_vector_fixed_protected_paths(case):
    assert (sp.fixed_protected(case["path"]) is not None) is case["protected"]


@pytest.mark.parametrize("case", VECTORS["commands"],
                         ids=lambda case: f"{case['profile']}:{case['command'][:24]}")
def test_vector_commands(case):
    rules = VECTORS["profiles"][case["profile"]]
    assert sp.command_mode(rules, case["command"])[0] == case["mode"]


@pytest.mark.parametrize("case", VECTORS["paths"],
                         ids=lambda case: f"{case['profile']}:{case['path']}:{case['write']}")
def test_vector_paths(case, layout):
    rules = _fill(VECTORS["profiles"][case["profile"]], layout["out"])
    raw = case["path"].replace("/@out", layout["out"])
    assert sp.path_mode(rules, layout["ws"], raw, write=case["write"])[0] == case["mode"]


def test_builtins_are_valid_and_canonical():
    for rules in sp.BUILTINS.values():
        assert sp.validate(rules) == rules


def test_restricted_keys_name_what_a_plugin_must_enforce():
    assert sp.restricted_keys(sp.BUILTINS["trusted"]) == ["write_outside"]
    assert sp.restricted_keys(sp.BUILTINS["read-only"]) == [
        "read_outside", "write_inside", "write_outside", "shell", "web", "extensions"]
    rules = sp.validate({**sp.BUILTINS["trusted"], "protected": ["dist/"],
                         "external_roots": [{"path": "/srv", "read": "deny"}],
                         "commands": [{"prefix": "git push", "mode": "ask"}]})
    assert sp.restricted_keys(rules)[-3:] == ["protected", "external_roots", "commands"]
    # A root or command rule looser than its category needs no enforcement.
    loose = sp.validate({**sp.BUILTINS["read-only"],
                         "external_roots": [{"path": "/srv", "read": "allow"}],
                         "commands": [{"prefix": "ls", "mode": "allow"}]})
    assert "external_roots" not in sp.restricted_keys(loose)
    assert "commands" not in sp.restricted_keys(loose)
    codex = ["write_inside", "write_outside", "protected", "commands"]
    assert sp.unenforced(sp.BUILTINS["trusted"], codex) == []
    assert "shell" in sp.unenforced(sp.BUILTINS["reviewed"], codex)


def test_loosened_reports_every_widening():
    old = sp.validate({**sp.BUILTINS["reviewed"], "protected": ["a"],
                       "external_roots": [{"path": "/srv", "write": "ask"}],
                       "commands": [{"prefix": "git push", "mode": "deny"}]})
    assert sp.loosened(old, old) == []
    new = sp.validate({**sp.BUILTINS["reviewed"], "shell": "allow",
                       "external_roots": [{"path": "/srv", "write": "allow"}]})
    assert sp.loosened(old, new) == ["shell", "protected", "external_roots", "commands"]
    assert sp.loosened(new, old) == []


def test_wire_round_trip_and_tampering():
    block = sp.wire("reviewed", sp.BUILTINS["reviewed"])
    assert sp.from_wire(block) == ("reviewed", sp.revision(sp.BUILTINS["reviewed"]),
                                   sp.BUILTINS["reviewed"])
    tampered = json.loads(json.dumps(block))
    tampered["profile"]["rules"]["shell"] = "allow"
    for bad in (tampered, {"source": "native"}, None,
                {"source": "profile", "profile": {**block["profile"], "id": "Bad ID"}}):
        with pytest.raises(BridgeError):
            sp.from_wire(bad)


def test_other_users_home_is_refused(tmp_path):
    assert sp.resolve_path(str(tmp_path), "~root/x") is None
    assert sp.resolve_path(str(tmp_path), "~/x") == os.path.realpath(os.path.expanduser("~/x"))


# ---------------------------------------------------------------------------
# Profile registry

CUSTOM = {**sp.BUILTINS["reviewed"], "protected": ["secrets/"]}


def _route(env, adapter_id, **kwargs):
    service = env["service"]
    return service.set_workspace_route(service.workspace(env["id"]), adapter_id, True, **kwargs)


def test_registry_lists_builtins_and_saves_custom_profiles(env):
    profiles = env["service"].profiles
    assert [row["id"] for row in profiles.list()] == ["read-only", "reviewed", "trusted"]
    saved = profiles.save("team", CUSTOM)
    assert saved["saved"] and saved["revision"] == sp.revision(sp.validate(CUSTOM))
    assert profiles.get("team")["rules"]["protected"] == ["secrets/"]
    with pytest.raises(BridgeError) as caught:
        profiles.save("team", CUSTOM)
    assert caught.value.code == "conflict"
    with pytest.raises(BridgeError) as caught:
        profiles.save("team", CUSTOM, expected_revision="prof_stale")
    assert caught.value.code == "conflict"
    for name in ("trusted", "native", "Bad"):
        with pytest.raises(BridgeError):
            profiles.save(name, CUSTOM)


def test_registry_asks_before_loosening_and_tracks_routes(env):
    service = env["service"]
    adapter = create_test_adapter(service, env["node_id"], {"name": "Pi", "runtime_type": "pi"})
    profile = service.profiles.save("team", CUSTOM)
    _route(env, adapter["id"], profile_id="team")
    assert service.profiles.routes_using("team")[0]["adapter_id"] == adapter["id"]
    looser = {**CUSTOM, "shell": "allow"}
    pending = service.profiles.save("team", looser, expected_revision=profile["revision"])
    assert pending == {"saved": False, "needs_confirmation": True, "id": "team",
                       "loosened": ["shell"], "routes": service.profiles.routes_using("team"),
                       "unenforceable_routes": []}
    assert service.profiles.get("team")["revision"] == profile["revision"]
    saved = service.profiles.save("team", looser, expected_revision=profile["revision"],
                                  confirm=True)
    assert saved["saved"] and saved["rules"]["shell"] == "allow"
    with pytest.raises(BridgeError) as caught:
        service.profiles.delete("team")
    assert caught.value.code == "conflict"
    _route(env, adapter["id"], profile_id="read-only")
    assert service.profiles.delete("team") == {"deleted": True, "id": "team"}


def test_route_refuses_a_profile_its_agent_cannot_enforce(env):
    service = env["service"]
    codex = create_test_adapter(service, env["node_id"], {"name": "Codex", "runtime_type": "codex"})
    assert adapter_security(service.adapter_registry.get(codex["id"])) == {
        "enforces": sorted(["write_inside", "write_outside", "protected", "commands"]),
        "native": True, "worktrees": True}
    with pytest.raises(BridgeError) as caught:
        _route(env, codex["id"], profile_id="reviewed")
    assert caught.value.code == "security_unavailable"
    trusted = _route(env, codex["id"], profile_id="trusted")
    native = _route(env, codex["id"], security_source="native")
    assert trusted and native
    pi = create_test_adapter(service, env["node_id"], {"name": "Pi", "runtime_type": "pi"})
    with pytest.raises(BridgeError) as caught:
        _route(env, pi["id"], security_source="native")
    assert caught.value.code == "security_unavailable"


def test_binding_carries_the_current_rules(env):
    service = env["service"]
    pi = create_test_adapter(service, env["node_id"], {"name": "Pi", "runtime_type": "pi"})
    row = service.adapter_registry.get(pi["id"])
    assert service.profiles.binding(row, "profile", "reviewed") == sp.wire(
        "reviewed", sp.BUILTINS["reviewed"])
    with pytest.raises(BridgeError) as caught:
        service.profiles.binding(row, None, None)
    assert caught.value.code == "profile_unconfigured"
