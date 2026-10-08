"""App-bound single-skill packaging with synthetic bindings and isolated state."""
import argparse
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
from zipfile import ZipFile

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/build_skill_plugin.py"
spec = importlib.util.spec_from_file_location("build_skill_plugin", SCRIPT)
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)
APP_ID = "asdk_app_example123"
TECHNICAL_ID = "plugin_" + APP_ID
PACKAGE_NAME = "registered-example-package"


@pytest.fixture(autouse=True)
def isolate_configuration(tmp_path, monkeypatch):
    # Never read a real user config or inherit real private binding settings.
    monkeypatch.setattr(packager, "DEFAULT_ENV_FILE", tmp_path / ".env.skill-plugin")
    for key in list(packager.os.environ):
        if key.startswith("WB_SKILL_PLUGIN_"):
            monkeypatch.delenv(key)


def write_env(path, **settings):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"{packager.ENV_OPTIONS[key]}={value}" for key, value in settings.items()) + "\n")
    return path


def skill_bytes(name="my-workflow", version="9.2.0", body="Use the connected MCP tools to review evidence."):
    own_version = f"version: {version}\n" if version is not None else ""
    return f"---\nname: {name}\ndescription: Review evidence with the connected app.\n{own_version}---\n\n{body}\n".encode()


@pytest.fixture
def initialized(tmp_path):
    directory = tmp_path / "state"
    source = tmp_path / "SKILL.md"
    source.write_bytes(skill_bytes())
    path = packager.initialize(TECHNICAL_ID, directory, PACKAGE_NAME, "1.4.9", skill_path=source)
    return directory, source, path


@pytest.fixture
def already_packaged(tmp_path):
    directory = tmp_path / "state"
    source = tmp_path / "SKILL.md"
    source.write_bytes(skill_bytes())
    path = packager.initialize(TECHNICAL_ID, directory, "My_Plugin-2", "1.4.9",
                               skill_path=source, skill_already_packaged=True)
    return directory, source, path


@pytest.mark.parametrize("value,expected", [
    (TECHNICAL_ID, APP_ID), (APP_ID, APP_ID),
    ("plugin_asdk_app_A0_example-x", "asdk_app_A0_example-x"),
])
def test_app_id_normalization(value, expected):
    assert packager.normalize_app_id(value) == expected


@pytest.mark.parametrize("value", [
    "", "plugin_asdk_app_", "asdk_app_-bad", "asdk_app__bad", "plugin_example",
    "dev-example", "connector_example", "https://example.com/plugin_asdk_app_example",
    " asdk_app_example", "asdk_app_example\n", "asdk_app_../private", "asdk_app_example?key=value", None,
])
def test_invalid_app_ids_fail_without_echoing_input(value):
    with pytest.raises(packager.PackagingError, match="app ID must be"):
        packager.normalize_app_id(value)


@pytest.mark.parametrize("name", ["A", "1", "My_Plugin-2", "Example__--", "name-", "name_", "a" * 52])
def test_valid_package_names_are_preserved_in_state_and_both_manifests(tmp_path, name):
    directory = tmp_path / "state"
    source = tmp_path / "SKILL.md"
    source.write_bytes(skill_bytes())
    path = packager.initialize(APP_ID, directory, name, "1.0.0", skill_path=source)
    output, version = packager.build(APP_ID, directory)
    assert version == "1.0.1" and output.name == f"{name}-1.0.1.zip"
    assert json.loads(path.read_bytes())["package_name"] == name
    with ZipFile(output) as archive:
        for manifest in ("plugin.json", ".codex-plugin/plugin.json"):
            assert json.loads(archive.read(manifest))["name"] == name


@pytest.mark.parametrize("name", ["", "a" * 65, "my.plugin", ".plugin", "_plugin", "-plugin",
                                     "my plugin", "my/plugin", "éplugin", "plugín", "name\n", None, 42])
def test_invalid_package_names_fail_before_state_creation(tmp_path, name):
    with pytest.raises(packager.PackagingError, match="bootstrap requires"):
        packager.initialize(APP_ID, tmp_path / "state", name, "1.0.0")
    assert not (tmp_path / "state").exists()


def test_template_names_use_package_rules_and_aliases_keep_separate_rules(tmp_path):
    template = json.loads(packager.DEFAULT_TEMPLATE.read_bytes())
    template["name"] = "My_Template-2"
    source = tmp_path / "template.json"
    source.write_text(json.dumps(template))
    path = packager.initialize(APP_ID, tmp_path / "state", "My_Plugin-2", "1.0.0",
                               template=source, app_alias="example")
    assert json.loads(path.read_bytes())["package_name"] == "My_Plugin-2"
    with pytest.raises(packager.PackagingError, match="app alias"):
        packager.initialize(APP_ID, tmp_path / "other-state", "My_Plugin-2", "1.0.0",
                            app_alias="My_Alias")
    template["name"] = "invalid.name"
    source.write_text(json.dumps(template))
    with pytest.raises(packager.PackagingError, match="template requires"):
        packager.initialize(APP_ID, tmp_path / "invalid-state", "My_Plugin-2", "1.0.0",
                            template=source, app_alias="example")


@pytest.mark.parametrize("already_present", [False, True])
def test_combined_identity_exactly_64_can_be_baselined_and_packaged(tmp_path, already_present):
    directory = tmp_path / "state"
    source = tmp_path / "custom-skill.md"
    package_name, name = "P" * 32, "s" * 31
    assert len(f"{package_name}:{name}") == 64
    source.write_bytes(skill_bytes(name))
    path = packager.initialize(APP_ID, directory, package_name, "1.0.0", skill_path=source,
                               skill_already_packaged=already_present)
    if already_present:
        before = path.read_bytes()
        assert packager.build(APP_ID, directory) is None
        assert path.read_bytes() == before and not list(directory.rglob("*.zip"))
        source.write_bytes(source.read_bytes() + b"\nChanged instructions.\n")
    output, version = packager.build(APP_ID, directory)
    assert version == "1.0.1" and len(list(directory.rglob("*.zip"))) == 1
    with ZipFile(output) as archive:
        assert json.loads(archive.read("plugin.json"))["name"] == package_name
        assert archive.read(f"skills/{name}/SKILL.md") == source.read_bytes()
    assert packager.build(APP_ID, directory) is None


@pytest.mark.parametrize("override", [False, True])
def test_overlong_changed_identity_leaves_state_and_output_untouched(already_packaged, tmp_path, override):
    directory, source, path = already_packaged
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    name = "s" * 53
    assert len(f"My_Plugin-2:{name}") == 65
    selected = tmp_path / "custom-skill.md" if override else source
    selected.write_bytes(skill_bytes(name))
    requested = tmp_path / "unused.zip"
    with pytest.raises(packager.PackagingError, match="combined plugin-name:skill-name identity.*64"):
        packager.build(APP_ID, directory, skill_path=selected if override else None, output=requested)
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    assert json.loads(path.read_bytes())["package_version"] == "1.4.9"
    assert not requested.exists() and not list(directory.rglob("*.zip"))
    assert not (directory / "artifacts").exists()


def test_individual_name_limits_do_not_override_combined_identity_limit(tmp_path):
    directory = tmp_path / "state"
    source = tmp_path / "custom-skill.md"
    source.write_bytes(skill_bytes("s"))
    # A 64-character package name is individually valid, but no skill fits with it.
    path = packager.initialize(APP_ID, directory, "P" * 64, "1.0.0", skill_path=source)
    before = path.read_bytes()
    with pytest.raises(packager.PackagingError, match="combined plugin-name:skill-name identity.*64"):
        packager.build(APP_ID, directory)
    assert path.read_bytes() == before and not list(directory.rglob("*.zip"))
    assert not (directory / "artifacts").exists()


def test_already_packaged_bootstrap_rejects_identity_65_before_state_creation(tmp_path):
    directory = tmp_path / "state"
    source = tmp_path / "custom-skill.md"
    source.write_bytes(skill_bytes("s" * 32))
    assert len(f"{'P' * 32}:{'s' * 32}") == 65
    with pytest.raises(packager.PackagingError, match="combined plugin-name:skill-name identity.*64"):
        packager.initialize(APP_ID, directory, "P" * 32, "1.0.0", skill_path=source,
                            skill_already_packaged=True)
    assert not directory.exists() and not list(tmp_path.rglob("*.zip"))


def test_overlong_identity_is_rejected_before_unchanged_digest_noop(tmp_path):
    directory = tmp_path / "state"
    source = tmp_path / "custom-skill.md"
    source.write_bytes(skill_bytes("s" * 32))
    path = packager.initialize(APP_ID, directory, "P" * 32, "1.0.0", skill_path=source)
    # Simulate an existing-release baseline accepted by the previous builder.
    value = json.loads(path.read_bytes())
    value.update(skill_baseline_origin="existing_release", skill_name="s" * 32,
                 skill_version="9.2.0", last_skill_sha256=sha256(source.read_bytes()).hexdigest())
    path.write_text(json.dumps(value))
    before = path.read_bytes()
    with pytest.raises(packager.PackagingError, match="combined plugin-name:skill-name identity.*64"):
        packager.build(APP_ID, directory)
    assert path.read_bytes() == before and not list(directory.rglob("*.zip"))
    assert not (directory / "artifacts").exists()


def test_bootstrap_records_exact_identity_and_template_alias(initialized):
    directory, _, path = initialized
    state = json.loads(path.read_bytes())
    assert path == packager.state_path(directory, APP_ID)
    assert state["app_id"] == APP_ID
    assert state["package_name"] == state["manifest"]["name"] == PACKAGE_NAME
    assert state["package_version"] == state["manifest"]["version"] == "1.4.9"
    assert state["app_alias"] == "workspace-bridge"
    assert state["skill_baseline_origin"] == "unpackaged"
    assert state["last_skill_sha256"] is state["last_artifact"] is None
    assert not list(directory.rglob("*.zip"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_first_build_has_portable_layout_binding_and_synchronized_identity(initialized):
    directory, source, path = initialized
    output, version = packager.build(APP_ID, directory, skill_path=source)
    assert version == "1.4.10"
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            "plugin.json", ".codex-plugin/plugin.json", ".app.json", "skills/my-workflow/SKILL.md",
        }
        assert archive.testzip() is None
        root = json.loads(archive.read("plugin.json"))
        legacy = json.loads(archive.read(".codex-plugin/plugin.json"))
        assert root["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
        assert root["name"] == legacy["name"] == PACKAGE_NAME
        assert root["version"] == legacy["version"] == version
        assert root["extensions"]["com.openai"]["apps"] == legacy["apps"] == "./.app.json"
        assert root["extensions"]["com.openai"]["interface"] == legacy["interface"]
        assert legacy["skills"] == "./skills/" and "skills" not in root
        assert json.loads(archive.read(".app.json")) == {"apps": {"workspace-bridge": {"id": APP_ID}}}
        assert archive.read("skills/my-workflow/SKILL.md") == source.read_bytes()
    state = json.loads(path.read_bytes())
    assert state["last_skill_sha256"] == sha256(source.read_bytes()).hexdigest()
    assert state["skill_baseline_origin"] == "generated"
    assert state["skill_version"] == "9.2.0"  # Independent of package version.
    assert state["last_artifact"] == {
        "path": str(output), "version": version, "sha256": sha256(output.read_bytes()).hexdigest(),
    }
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_already_packaged_bootstrap_is_an_exact_noop_without_local_artifact(already_packaged, tmp_path):
    directory, source, path = already_packaged
    state = json.loads(path.read_bytes())
    assert state["skill_baseline_origin"] == "existing_release"
    assert state["last_skill_sha256"] == sha256(source.read_bytes()).hexdigest()
    assert state["skill_name"] == "my-workflow" and state["skill_version"] == "9.2.0"
    assert state["last_artifact"] is None
    assert state["package_version"] == state["manifest"]["version"] == "1.4.9"
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    requested = tmp_path / "unused.zip"
    assert packager.build(APP_ID, directory, output=requested) is None
    assert not requested.exists() and not list(directory.rglob("*.zip"))
    assert not (directory / "artifacts").exists()
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime


def test_changed_bytes_after_existing_release_baseline_bump_once(already_packaged):
    directory, source, path = already_packaged
    source.write_bytes(source.read_bytes() + b"\nRequire another evidence check.\n")
    output, version = packager.build(TECHNICAL_ID, directory)
    assert version == "1.4.10"
    state = json.loads(path.read_bytes())
    assert state["skill_baseline_origin"] == "generated"
    assert state["last_skill_sha256"] == sha256(source.read_bytes()).hexdigest()
    assert state["last_artifact"] == {
        "path": str(output), "version": version, "sha256": sha256(output.read_bytes()).hexdigest(),
    }
    with ZipFile(output) as archive:
        assert archive.read("skills/my-workflow/SKILL.md") == source.read_bytes()
        for manifest in ("plugin.json", ".codex-plugin/plugin.json"):
            value = json.loads(archive.read(manifest))
            assert value["name"] == "My_Plugin-2" and value["version"] == version
    before = path.read_bytes()
    assert packager.build(APP_ID, directory) is None
    assert path.read_bytes() == before and len(list(directory.rglob("*.zip"))) == 1


@pytest.mark.parametrize("content", [b"invalid skill", b"---\nname: unsafe_name\ndescription: fine\n---\nUse tools."])
def test_already_packaged_bootstrap_validates_skill_before_creating_state(tmp_path, content):
    source = tmp_path / "SKILL.md"
    source.write_bytes(content)
    with pytest.raises(packager.PackagingError):
        packager.initialize(APP_ID, tmp_path / "state", PACKAGE_NAME, "1.0.0",
                            skill_path=source, skill_already_packaged=True)
    assert not (tmp_path / "state").exists()


def test_failed_build_from_existing_release_baseline_preserves_imported_state(already_packaged, monkeypatch):
    directory, source, path = already_packaged
    before = path.read_bytes()
    source.write_bytes(source.read_bytes() + b"\nChanged instructions.\n")
    def fail(*args, **kwargs):
        raise packager.PackagingError("synthetic archive failure")
    monkeypatch.setattr(packager, "_validate_archive", fail)
    with pytest.raises(packager.PackagingError, match="synthetic archive failure"):
        packager.build(APP_ID, directory)
    assert path.read_bytes() == before and not list(directory.rglob("*.zip"))


def test_identical_skill_is_exact_noop_even_with_requested_output(initialized, tmp_path):
    directory, source, path = initialized
    output, _ = packager.build(TECHNICAL_ID, directory, skill_path=source)
    before_state, before_zip = path.read_bytes(), output.read_bytes()
    state_mtime, zip_mtime = path.stat().st_mtime_ns, output.stat().st_mtime_ns
    requested = tmp_path / "unused.zip"
    assert packager.build(APP_ID, directory, skill_path=source, output=requested) is None
    assert not requested.exists()
    requested.write_bytes(b"unrelated existing file")
    assert packager.build(APP_ID, directory, skill_path=source, output=requested) is None
    assert requested.read_bytes() == b"unrelated existing file"
    assert path.read_bytes() == before_state and output.read_bytes() == before_zip
    assert path.stat().st_mtime_ns == state_mtime and output.stat().st_mtime_ns == zip_mtime
    assert len(list(directory.rglob("*.zip"))) == 1


def test_changed_skill_bumps_patch_once_without_requiring_skill_version_change(initialized):
    directory, source, path = initialized
    first, _ = packager.build(APP_ID, directory, skill_path=source)
    source.write_bytes(source.read_bytes() + b"\nRequire explicit evidence for each conclusion.\n")
    second, version = packager.build(TECHNICAL_ID, directory, skill_path=source)
    assert version == "1.4.11" and second != first
    assert packager.build(APP_ID, directory, skill_path=source) is None
    assert json.loads(path.read_bytes())["package_version"] == "1.4.11"
    assert len(list(directory.rglob("*.zip"))) == 2
    with ZipFile(second) as archive:
        assert archive.read("skills/my-workflow/SKILL.md") == source.read_bytes()


def test_generic_template_and_skill_are_snapshotted_and_deterministic(tmp_path):
    template = json.loads(packager.DEFAULT_TEMPLATE.read_bytes())
    template.update(name="custom-template", description="Custom workflow.", author={"name": "Example team"})
    interface = {"displayName": "Example Workflow", "defaultPrompt": ["Review", "Inspect", "Explain"]}
    template["extensions"]["com.openai"]["interface"] = interface
    template["extensions"]["org.example"] = {"metadata": [False, None, "preserved"]}
    template_path = tmp_path / "custom.json"
    template_path.write_text(json.dumps(template))
    source = tmp_path / "custom.md"
    content = skill_bytes("custom-workflow", version=None).replace(b"\n", b"\r\n")
    source.write_bytes(content)
    outputs = []
    for label in ("one", "two"):
        directory = tmp_path / label
        packager.initialize(APP_ID, directory, "exact-custom-name", "0.3.0", template=template_path,
                            app_alias="support", skill_path=source)
        outputs.append(packager.build(APP_ID, directory)[0])
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    # Future normal builds use the saved presentation; no template/cache lookup is needed.
    template_path.unlink()
    with ZipFile(outputs[0]) as archive:
        root = json.loads(archive.read("plugin.json"))
        assert root["name"] == "exact-custom-name" and root["version"] == "0.3.1"
        assert root["extensions"]["com.openai"]["interface"] == interface
        assert root["extensions"]["org.example"] == template["extensions"]["org.example"]
        assert json.loads(archive.read(".app.json")) == {"apps": {"support": {"id": APP_ID}}}
        assert archive.read("skills/custom-workflow/SKILL.md") == content
    assert packager.build(APP_ID, tmp_path / "one") is None


def test_default_template_has_no_user_binding_and_is_not_mutated(initialized):
    before = packager.DEFAULT_TEMPLATE.read_bytes()
    assert b"asdk_app_" not in before and b"dev-" not in before
    directory, source, _ = initialized
    packager.build(APP_ID, directory, skill_path=source)
    assert packager.DEFAULT_TEMPLATE.read_bytes() == before


@pytest.mark.parametrize("phase", ["write", "validate"])
def test_failed_archive_creation_never_advances_state(initialized, monkeypatch, phase):
    directory, source, path = initialized
    before = path.read_bytes()
    if phase == "write":
        def fail(*args, **kwargs):
            raise OSError("synthetic write failure")
        monkeypatch.setattr(packager.os, "link", fail)
    else:
        def fail(*args, **kwargs):
            raise packager.PackagingError("synthetic validation failure")
        monkeypatch.setattr(packager, "_validate_archive", fail)
    with pytest.raises((OSError, packager.PackagingError)):
        packager.build(APP_ID, directory, skill_path=source)
    assert path.read_bytes() == before
    assert not list(directory.rglob("*.zip"))
    assert not list(directory.rglob(".skill-plugin-*"))


def test_state_save_failure_keeps_archive_and_retry_reuses_same_version(initialized, monkeypatch):
    directory, source, path = initialized
    before = path.read_bytes()
    original = packager._save_state
    def fail(*args, **kwargs):
        raise OSError("synthetic state failure")
    monkeypatch.setattr(packager, "_save_state", fail)
    with pytest.raises(packager.PackagingError, match="ZIP was written and validated"):
        packager.build(APP_ID, directory, skill_path=source)
    assert path.read_bytes() == before
    [created] = list(directory.rglob("*.zip"))
    old_bytes, old_mtime = created.read_bytes(), created.stat().st_mtime_ns
    monkeypatch.setattr(packager, "_save_state", original)
    output, version = packager.build(APP_ID, directory, skill_path=source)
    assert output == created and version == "1.4.10"
    assert created.read_bytes() == old_bytes and created.stat().st_mtime_ns == old_mtime
    assert json.loads(path.read_bytes())["package_version"] == "1.4.10"
    assert packager.build(APP_ID, directory, skill_path=source) is None


def test_failed_atomic_state_replace_keeps_old_state_and_removes_staging_file(initialized, monkeypatch):
    directory, _, path = initialized
    before = path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError("synthetic replace failure")
    monkeypatch.setattr(packager.os, "replace", fail)
    with pytest.raises(OSError):
        packager._save_state(path, {"changed": True})
    assert path.read_bytes() == before
    assert not list(directory.glob(".skill-state-*"))


def test_output_collision_does_not_overwrite_or_advance_state(initialized, tmp_path):
    directory, source, path = initialized
    before = path.read_bytes()
    output = tmp_path / "unrelated.zip"
    output.write_bytes(b"unrelated work")
    with pytest.raises(packager.PackagingError, match="different content"):
        packager.build(APP_ID, directory, skill_path=source, output=output)
    assert output.read_bytes() == b"unrelated work" and path.read_bytes() == before
    link = tmp_path / "linked.zip"
    link.symlink_to(output)
    with pytest.raises(packager.PackagingError, match="symlink"):
        packager.build(APP_ID, directory, skill_path=source, output=link)
    assert output.read_bytes() == b"unrelated work" and path.read_bytes() == before


def test_missing_output_directory_does_not_advance_state(initialized, tmp_path):
    directory, source, path = initialized
    before = path.read_bytes()
    with pytest.raises(OSError):
        packager.build(APP_ID, directory, skill_path=source, output=tmp_path / "missing/result.zip")
    assert path.read_bytes() == before


def test_missing_state_fails_with_precise_bootstrap_instruction(tmp_path):
    directory = tmp_path / "missing-state"
    with pytest.raises(packager.PackagingError, match="--init --package-name EXACT_EXISTING_NAME --package-version CURRENT_VERSION"):
        packager.build(APP_ID, directory)
    assert not directory.exists()


@pytest.mark.parametrize("change", [
    {"format_version": True}, {"format_version": 2}, {"app_id": "asdk_app_another"},
    {"package_name": "different"}, {"package_version": "1.4.10"}, {"package_version": None},
    {"last_skill_sha256": "invalid"}, {"last_artifact": {}}, {"skill_name": "unexpected"},
    {"skill_path": "relative/SKILL.md"},
    {"skill_baseline_origin": None}, {"skill_baseline_origin": "existing_release"},
])
def test_invalid_or_ambiguous_state_fails_closed(initialized, change):
    directory, source, path = initialized
    value = json.loads(path.read_bytes())
    value.update(change)
    path.write_text(json.dumps(value))
    before = path.read_bytes()
    with pytest.raises(packager.PackagingError, match="state is invalid.*trusted backup"):
        packager.build(APP_ID, directory, skill_path=source)
    assert path.read_bytes() == before and not list(directory.rglob("*.zip"))


@pytest.mark.parametrize("change", [
    {"skill_baseline_origin": "unpackaged"}, {"skill_baseline_origin": "generated"},
    {"skill_baseline_origin": "unknown"}, {"skill_baseline_origin": None},
    {"last_skill_sha256": None}, {"last_skill_sha256": "bad"},
    {"skill_name": None}, {"skill_version": 9}, {"last_artifact": {}},
])
def test_existing_release_baseline_rejects_inconsistent_fields(already_packaged, change):
    directory, _, path = already_packaged
    value = json.loads(path.read_bytes())
    value.update(change)
    path.write_text(json.dumps(value))
    before = path.read_bytes()
    with pytest.raises(packager.PackagingError, match="state is invalid"):
        packager.build(APP_ID, directory)
    assert path.read_bytes() == before and not list(directory.rglob("*.zip"))


@pytest.mark.parametrize("artifact", [None, {}, {"path": "/example.zip", "sha256": "bad", "version": "1.4.10"}])
def test_generated_state_still_requires_valid_artifact_metadata(initialized, artifact):
    directory, _, path = initialized
    packager.build(APP_ID, directory)
    value = json.loads(path.read_bytes())
    assert value["skill_baseline_origin"] == "generated"
    value["last_artifact"] = artifact
    path.write_text(json.dumps(value))
    before = path.read_bytes()
    with pytest.raises(packager.PackagingError, match="state is invalid"):
        packager.build(APP_ID, directory)
    assert path.read_bytes() == before and len(list(directory.rglob("*.zip"))) == 1


@pytest.mark.parametrize("generated", [False, True])
def test_prior_unmarked_states_keep_original_strict_artifact_rules(initialized, generated):
    directory, _, path = initialized
    if generated:
        packager.build(APP_ID, directory)
    value = json.loads(path.read_bytes())
    del value["skill_baseline_origin"]
    path.write_text(json.dumps(value))
    before = path.read_bytes()
    if generated:
        assert packager.build(APP_ID, directory) is None and path.read_bytes() == before
        value["last_artifact"] = None
        path.write_text(json.dumps(value))
        with pytest.raises(packager.PackagingError, match="state is invalid"):
            packager.build(APP_ID, directory)
    else:
        assert packager.build(APP_ID, directory)[1] == "1.4.10"
        assert json.loads(path.read_bytes())["skill_baseline_origin"] == "generated"


def test_existing_release_baseline_without_explicit_marker_is_rejected(already_packaged):
    directory, _, path = already_packaged
    value = json.loads(path.read_bytes())
    del value["skill_baseline_origin"]
    path.write_text(json.dumps(value))
    with pytest.raises(packager.PackagingError, match="state is invalid"):
        packager.build(APP_ID, directory)
    assert not list(directory.rglob("*.zip"))


@pytest.mark.parametrize("raw", [b"{not json", b'{"format_version": 1, "format_version": 2}', b'[]', b'\xff'])
def test_corrupt_state_is_not_repaired_implicitly(initialized, raw):
    directory, source, path = initialized
    path.write_bytes(raw)
    with pytest.raises(packager.PackagingError, match="state is invalid"):
        packager.build(APP_ID, directory, skill_path=source)
    assert path.read_bytes() == raw


def test_reinitialization_never_overwrites_existing_state(initialized):
    directory, _, path = initialized
    before = path.read_bytes()
    with pytest.raises(packager.PackagingError, match="state already exists"):
        packager.initialize(APP_ID, directory, "replacement-name", "7.0.0")
    assert path.read_bytes() == before


@pytest.mark.parametrize("version", ["1", "1.2", "01.2.3", "1.02.3", "1.2.03", "1.2.3-beta", "1.2.3+build", "1.2.3\n", "-1.2.3"])
def test_invalid_bootstrap_versions_fail_closed(tmp_path, version):
    with pytest.raises(packager.PackagingError, match="bootstrap requires"):
        packager.initialize(APP_ID, tmp_path / "state", PACKAGE_NAME, version)
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("content", [
    b"no frontmatter", b"---\nname: workflow\n", b"---\nname: ../unsafe\ndescription: fine\n---\nUse tools.",
    b"---\nname: Uppercase\ndescription: fine\n---\nUse tools.", b"---\nname: 123\ndescription: fine\n---\nUse tools.",
    b"---\nname: my_workflow\ndescription: fine\n---\nUse tools.", b"---\nname: my.workflow\ndescription: fine\n---\nUse tools.",
    b"---\nname: my--workflow\ndescription: fine\n---\nUse tools.", b"---\nname: workflow\n---\nUse tools.",
    b"---\nname: workflow\ndescription: fine\nname: other\n---\nUse tools.",
    b"---\nname: workflow\ndescription: [wrong, type]\n---\nUse tools.",
    b"---\nname: workflow\ndescription: fine\nversion: 1.0\n---\nUse tools.",
    b"---\nname: workflow\ndescription: fine\n---\n", b"\xff", skill_bytes() + b"\0",
])
def test_invalid_skill_frontmatter_never_creates_archive_or_updates_state(initialized, content):
    directory, source, path = initialized
    before = path.read_bytes()
    source.write_bytes(content)
    with pytest.raises(packager.PackagingError):
        packager.build(APP_ID, directory, skill_path=source)
    assert path.read_bytes() == before and not list(directory.rglob("*.zip"))


def test_concurrent_build_fails_without_state_mutation(initialized):
    directory, source, path = initialized
    before = path.read_bytes()
    with packager._state_lock(directory, APP_ID):
        with pytest.raises(packager.PackagingError, match="another packager"):
            packager.build(APP_ID, directory, skill_path=source)
    assert path.read_bytes() == before and not list(directory.rglob("*.zip"))


@pytest.mark.parametrize("change", [
    {"author": {"name": "Example", "extra": "unsupported"}}, {"version": "not-a-version"},
    {"interface": "unsupported"}, {"mcpServers": {}},
    {"name": "invalid.name"},
])
def test_invalid_templates_fail_without_initialization(tmp_path, change):
    template = json.loads(packager.DEFAULT_TEMPLATE.read_bytes())
    template.update(change)
    source = tmp_path / "template.json"
    source.write_text(json.dumps(template))
    with pytest.raises(packager.PackagingError):
        packager.initialize(APP_ID, tmp_path / "state", PACKAGE_NAME, "1.0.0", template=source)
    assert not (tmp_path / "state").exists()


def test_app_id_only_cli_build_after_bootstrap_uses_repository_canonical_skill(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "local-state"))
    assert packager.main(["--app-id", TECHNICAL_ID, "--init", "--package-name", PACKAGE_NAME,
                          "--package-version", "2.0.0"]) == 0
    assert packager.main(["--app-id", TECHNICAL_ID]) == 0
    [output] = list(tmp_path.rglob("*.zip"))
    with ZipFile(output) as archive:
        assert archive.read("skills/project-lead/SKILL.md") == packager.DEFAULT_SKILL.read_bytes()
    assert packager.main(["--app-id", APP_ID]) == 0
    stdout = capsys.readouterr().out
    assert "package version 2.0.1" in stdout and "No change:" in stdout
    assert APP_ID not in stdout
    assert len(list(tmp_path.rglob("*.zip"))) == 1


def test_cli_help_and_missing_bootstrap_are_safe(tmp_path):
    help_result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
    assert help_result.returncode == 0 and "--app-id" in help_result.stdout and "--init" in help_result.stdout
    assert "--skill-already-packaged" in help_result.stdout
    assert "--env-file" in help_result.stdout and "--no-skill-already-packaged" in help_result.stdout
    empty_config = write_env(tmp_path / "empty.env")
    result = subprocess.run([sys.executable, str(SCRIPT), "--app-id", TECHNICAL_ID,
                             "--env-file", str(empty_config), "--state", str(tmp_path / "missing")],
                            capture_output=True, text=True)
    assert result.returncode == 2 and "--package-name EXACT_EXISTING_NAME" in result.stderr
    assert APP_ID not in result.stderr and "Traceback" not in result.stderr


def test_already_packaged_cli_baseline_uses_canonical_skill_and_immediately_noops(tmp_path, capsys):
    directory = tmp_path / "state"
    assert packager.main(["--app-id", TECHNICAL_ID, "--state", str(directory), "--init",
                          "--package-name", "My_Plugin-2", "--package-version", "2.0.0",
                          "--skill-already-packaged"]) == 0
    path = packager.state_path(directory, APP_ID)
    before = path.read_bytes()
    assert packager.main(["--app-id", APP_ID, "--state", str(directory)]) == 0
    value = json.loads(path.read_bytes())
    assert value["last_skill_sha256"] == sha256(packager.DEFAULT_SKILL.read_bytes()).hexdigest()
    assert value["skill_name"] == "project-lead"
    assert value["package_version"] == "2.0.0" and value["last_artifact"] is None
    assert path.read_bytes() == before and not list(directory.rglob("*.zip"))
    stdout = capsys.readouterr().out
    assert "No change:" in stdout and "Created" not in stdout and APP_ID not in stdout


def test_already_packaged_flag_requires_init(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        packager.main(["--app-id", APP_ID, "--state", str(tmp_path / "state"), "--skill-already-packaged"])
    assert exc.value.code == 2 and "only accepted with --init" in capsys.readouterr().err
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("source", ["file", "process"])
def test_env_only_app_id_normal_build_uses_local_state(initialized, monkeypatch, source, capsys):
    directory, _, path = initialized
    if source == "file":
        write_env(packager.DEFAULT_ENV_FILE, app_id=TECHNICAL_ID, state=directory)
    else:
        monkeypatch.setenv("WB_SKILL_PLUGIN_APP_ID", TECHNICAL_ID)
        monkeypatch.setenv("WB_SKILL_PLUGIN_STATE", str(directory))
    assert packager.main([]) == 0
    assert json.loads(path.read_bytes())["package_version"] == "1.4.10"
    before = path.read_bytes()
    assert packager.main([]) == 0
    assert path.read_bytes() == before and len(list(directory.rglob("*.zip"))) == 1
    stdout = capsys.readouterr().out
    assert "No change:" in stdout and APP_ID not in stdout


def test_default_config_bootstrap_and_relative_paths_cover_generic_workflow(tmp_path, capsys):
    config = packager.DEFAULT_ENV_FILE
    source = tmp_path / "skills/SKILL.md"
    source.parent.mkdir()
    source.write_bytes(skill_bytes("custom-review"))
    template = tmp_path / "template.json"
    metadata = json.loads(packager.DEFAULT_TEMPLATE.read_bytes())
    metadata["description"] = "Custom configured presentation."
    template.write_text(json.dumps(metadata))
    (tmp_path / "output").mkdir()
    write_env(config, app_id=TECHNICAL_ID, package_name="Env_Plugin-2", package_version="1.5.0",
              skill_already_packaged="yes", state="private-state", app_alias="configured",
              template="template.json", skill="skills/SKILL.md", output="output/upload.zip")
    assert packager.main(["--init"]) == 0
    directory = tmp_path / "private-state"
    path = packager.state_path(directory, APP_ID)
    state = json.loads(path.read_bytes())
    assert state["skill_baseline_origin"] == "existing_release"
    assert state["package_name"] == "Env_Plugin-2" and state["package_version"] == "1.5.0"
    assert state["skill_path"] == str(source)
    assert state["manifest"]["description"] == metadata["description"]
    assert state["app_alias"] == "configured" and state["last_artifact"] is None
    before = path.read_bytes()
    assert packager.main([]) == 0  # Bootstrap-only file settings stay harmless here.
    assert path.read_bytes() == before and not list(tmp_path.rglob("*.zip"))
    source.write_bytes(source.read_bytes() + b"\nChanged instructions.\n")
    assert packager.main([]) == 0
    output = tmp_path / "output/upload.zip"
    with ZipFile(output) as archive:
        assert archive.read("skills/custom-review/SKILL.md") == source.read_bytes()
        assert json.loads(archive.read(".app.json")) == {"apps": {"configured": {"id": APP_ID}}}
    assert json.loads(path.read_bytes())["package_version"] == "1.5.1"
    assert APP_ID not in capsys.readouterr().out


def test_explicit_env_file_does_not_read_default_config(initialized, tmp_path):
    directory, _, _ = initialized
    packager.DEFAULT_ENV_FILE.write_text("broken default file must not be read")
    alternate = write_env(tmp_path / "alternate/config.env", app_id=TECHNICAL_ID, state=directory)
    assert packager.main(["--env-file", str(alternate)]) == 0
    assert len(list(directory.rglob("*.zip"))) == 1


@pytest.mark.parametrize("level", ["file", "process", "cli"])
def test_all_configuration_precedence_levels(tmp_path, monkeypatch, level):
    values = {}
    for index, source in enumerate(("file", "process", "cli"), 1):
        values[source] = {
            "app_id": f"plugin_asdk_app_{source}123", "package_name": f"{source}_Plugin",
            "package_version": f"{index}.0.0", "skill_already_packaged": source == "process",
            "app_alias": source,
            **{key: Path(f"{source}-{key}") for key in ("state", "template", "skill", "output")},
        }
    config = write_env(tmp_path / "config-dir/config.env", **values["file"])
    if level in ("process", "cli"):
        for option, value in values["process"].items():
            monkeypatch.setenv(packager.ENV_OPTIONS[option], str(value))
    args = argparse.Namespace(env_file=config, init=True,
                              **{option: values["cli"][option] if level == "cli" else None for option in packager.ENV_OPTIONS})
    resolved = packager.resolve_configuration(args)
    for option, expected in values[level].items():
        if option in packager.PATH_OPTIONS and level == "file":
            expected = (config.parent / expected).resolve()
        assert getattr(resolved, option) == expected


def test_cli_overrides_process_and_file_in_real_bootstrap(tmp_path, monkeypatch):
    directory = tmp_path / "chosen-state"
    source = tmp_path / "SKILL.md"
    source.write_bytes(skill_bytes())
    write_env(packager.DEFAULT_ENV_FILE, app_id="plugin_asdk_app_file123", package_name="File_Plugin",
              package_version="1.0.0", skill_already_packaged="true", state=tmp_path / "file-state")
    monkeypatch.setenv("WB_SKILL_PLUGIN_APP_ID", "plugin_asdk_app_process123")
    monkeypatch.setenv("WB_SKILL_PLUGIN_PACKAGE_NAME", "Process_Plugin")
    monkeypatch.setenv("WB_SKILL_PLUGIN_PACKAGE_VERSION", "2.0.0")
    monkeypatch.setenv("WB_SKILL_PLUGIN_STATE", str(tmp_path / "process-state"))
    monkeypatch.setenv("WB_SKILL_PLUGIN_ALREADY_PACKAGED", "true")
    assert packager.main(["--init", "--app-id", TECHNICAL_ID, "--package-name", "CLI_Plugin",
                          "--package-version", "3.0.0", "--state", str(directory), "--skill", str(source),
                          "--no-skill-already-packaged"]) == 0
    state = json.loads(packager.state_path(directory, APP_ID).read_bytes())
    assert state["package_name"] == "CLI_Plugin" and state["package_version"] == "3.0.0"
    assert state["skill_baseline_origin"] == "unpackaged" and state["last_skill_sha256"] is None
    assert not (tmp_path / "file-state").exists() and not (tmp_path / "process-state").exists()
    assert packager.main(["--app-id", TECHNICAL_ID, "--state", str(directory)]) == 0
    assert json.loads(packager.state_path(directory, APP_ID).read_bytes())["package_version"] == "3.0.1"


def test_process_and_cli_paths_keep_cwd_semantics(tmp_path, monkeypatch):
    working = tmp_path / "working"
    working.mkdir()
    monkeypatch.chdir(working)
    config = write_env(tmp_path / "config-dir/config.env", state="file-state", output="file.zip")
    monkeypatch.setenv("WB_SKILL_PLUGIN_STATE", "process-state")
    args = argparse.Namespace(env_file=config, init=False, **{key: None for key in packager.ENV_OPTIONS})
    args.output = Path("cli.zip")
    resolved = packager.resolve_configuration(args)
    assert resolved.state == Path("process-state") and resolved.output == Path("cli.zip")
    assert working / resolved.state != config.parent / "file-state"


def test_env_output_path_preserves_existing_symlink_rejection(initialized, tmp_path, capsys):
    directory, _, state = initialized
    before = state.read_bytes()
    target = tmp_path / "new-target.zip"
    link = tmp_path / "linked-output.zip"
    link.symlink_to(target)
    write_env(packager.DEFAULT_ENV_FILE, app_id=TECHNICAL_ID, state=directory, output="linked-output.zip")
    assert packager.main([]) == 2
    assert "output must not be a symlink" in capsys.readouterr().err
    assert link.is_symlink() and not target.exists() and state.read_bytes() == before


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("YeS", True), ("on", True),
    ("false", False), ("FALSE", False), ("0", False), ("no", False), ("No", False), ("off", False),
])
def test_env_boolean_values(tmp_path, raw, expected):
    config = write_env(tmp_path / "settings.env", skill_already_packaged=raw)
    assert packager.read_env_file(config)["WB_SKILL_PLUGIN_ALREADY_PACKAGED"] is expected


@pytest.mark.parametrize("raw", ["", "maybe", "2", "null", TECHNICAL_ID])
@pytest.mark.parametrize("source", ["file", "process"])
def test_invalid_booleans_are_rejected_without_echoing_input(tmp_path, monkeypatch, capsys, raw, source):
    if source == "file":
        write_env(packager.DEFAULT_ENV_FILE, app_id=TECHNICAL_ID, skill_already_packaged=raw)
    else:
        monkeypatch.setenv("WB_SKILL_PLUGIN_APP_ID", TECHNICAL_ID)
        monkeypatch.setenv("WB_SKILL_PLUGIN_ALREADY_PACKAGED", raw)
    assert packager.main([]) == 2
    captured = capsys.readouterr()
    assert "must be true/false" in captured.err and APP_ID not in captured.err + captured.out


def test_env_parser_supports_literals_export_comments_and_ignored_keys(tmp_path):
    path = tmp_path / "settings.env"
    path.write_text("\ufeff# comments and blanks\n\n"
                    f"export WB_SKILL_PLUGIN_APP_ID='{TECHNICAL_ID}' # inline comment\n"
                    'WB_SKILL_PLUGIN_PACKAGE_NAME="My_Plugin-2"\n'
                    "WB_SKILL_PLUGIN_PACKAGE_VERSION=1.0.0 # comment\n"
                    'WB_SKILL_PLUGIN_STATE="folder with spaces"\n'
                    "WB_SKILL_PLUGIN_APP_ALIAS=literal#hash\n"
                    "IGNORED=value\nIGNORED=another\n")
    parsed = packager.read_env_file(path)
    assert parsed["WB_SKILL_PLUGIN_APP_ID"] == TECHNICAL_ID
    assert parsed["WB_SKILL_PLUGIN_PACKAGE_NAME"] == "My_Plugin-2"
    assert parsed["WB_SKILL_PLUGIN_PACKAGE_VERSION"] == "1.0.0"
    assert parsed["WB_SKILL_PLUGIN_STATE"] == tmp_path / "folder with spaces"
    assert parsed["WB_SKILL_PLUGIN_APP_ALIAS"] == "literal#hash" and "IGNORED" not in parsed


@pytest.mark.parametrize("content", [
    b"malformed line", b"export bare", b"1INVALID=value", b"=empty-key",
    b"WB_SKILL_PLUGIN_APP_ID='unterminated", b'WB_SKILL_PLUGIN_APP_ID="value" trailing',
    b"WB_SKILL_PLUGIN_APP_ID=value\nWB_SKILL_PLUGIN_APP_ID=another\n",
    b"WB_SKILL_PLUGIN_ALREADY_PACKAGED=true\nWB_SKILL_PLUGIN_ALREADY_PACKAGED=false\n",
    b"WB_SKILL_PLUGIN_APP_ID=valid\0hidden", b"# ignored comment\0bad", b"\xff",
])
def test_malformed_duplicate_and_nul_env_files_fail_closed(tmp_path, content):
    path = tmp_path / "invalid.env"
    path.write_bytes(content)
    with pytest.raises(packager.PackagingError):
        packager.read_env_file(path)


@pytest.mark.parametrize("source", ["file", "process"])
def test_unknown_prefixed_keys_are_rejected_without_echoing_values(tmp_path, monkeypatch, capsys, source):
    key = "WB_SKILL_PLUGIN_APP_IID"
    if source == "file":
        packager.DEFAULT_ENV_FILE.write_text(f"{key}={TECHNICAL_ID}\n")
    else:
        monkeypatch.setenv(key, TECHNICAL_ID)
    assert packager.main([]) == 2
    captured = capsys.readouterr()
    assert "unknown WB_SKILL_PLUGIN_" in captured.err and APP_ID not in captured.err + captured.out


@pytest.mark.parametrize("expression", ["$HOME", "${HOME}", "$(touch SENTINEL)", "`touch SENTINEL`",
                                        "'$(touch SENTINEL)'", '"$(touch SENTINEL)"', "value; touch SENTINEL"])
def test_env_shell_syntax_is_rejected_and_never_executed(tmp_path, monkeypatch, expression):
    monkeypatch.chdir(tmp_path)
    write_env(packager.DEFAULT_ENV_FILE, app_id=TECHNICAL_ID, state=expression)
    assert packager.main(["--init"]) == 2
    assert not (tmp_path / "SENTINEL").exists() and not list(tmp_path.rglob("*.json"))


def test_quoted_backslashes_are_literal_and_not_unescaped(tmp_path):
    path = tmp_path / "literal.env"
    path.write_text("WB_SKILL_PLUGIN_STATE='folder\\nname'\n")
    assert packager.read_env_file(path)["WB_SKILL_PLUGIN_STATE"] == tmp_path / r"folder\nname"


def test_missing_explicit_env_file_fails_even_with_cli_app_id(tmp_path, capsys):
    assert packager.main(["--env-file", str(tmp_path / "missing.env"), "--app-id", TECHNICAL_ID]) == 2
    captured = capsys.readouterr()
    assert "explicit --env-file is missing" in captured.err and APP_ID not in captured.err + captured.out


def test_missing_default_file_and_docker_env_never_supply_configuration(tmp_path, capsys):
    assert not packager.DEFAULT_ENV_FILE.exists()
    (packager.DEFAULT_ENV_FILE.parent / ".env").write_text(f"WB_SKILL_PLUGIN_APP_ID={TECHNICAL_ID}\n")
    assert packager.main([]) == 2
    captured = capsys.readouterr()
    assert "app ID is required" in captured.err and APP_ID not in captured.err + captured.out


def test_env_settings_never_choose_initialization_implicitly(tmp_path, capsys):
    directory = tmp_path / "state"
    write_env(packager.DEFAULT_ENV_FILE, app_id=TECHNICAL_ID, package_name=PACKAGE_NAME,
              package_version="1.0.0", skill_already_packaged="true", state=directory)
    assert packager.main([]) == 2
    assert "packager state is missing" in capsys.readouterr().err and not directory.exists()
    with packager.DEFAULT_ENV_FILE.open("a") as file:
        file.write("WB_SKILL_PLUGIN_INIT=true\n")
    assert packager.main([]) == 2
    assert "unknown WB_SKILL_PLUGIN_" in capsys.readouterr().err and not directory.exists()


def test_example_config_is_synthetic_and_private_config_is_ignored():
    path = REPO / "packaging/chatgpt/skill-plugin.env.example"
    parsed = packager.read_env_file(path)
    assert parsed["WB_SKILL_PLUGIN_APP_ID"] == TECHNICAL_ID
    assert parsed["WB_SKILL_PLUGIN_ALREADY_PACKAGED"] is True
    assert subprocess.run(["git", "check-ignore", "--no-index", "--quiet", ".env.skill-plugin"], cwd=REPO).returncode == 0
    assert subprocess.run(["git", "check-ignore", "--no-index", "--quiet", str(path)], cwd=REPO).returncode == 1
