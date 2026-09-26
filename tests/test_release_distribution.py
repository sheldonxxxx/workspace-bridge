"""M4.2C1.1 CI/release distribution contracts (no live publish)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
RELEASE_YML = REPO / ".github" / "workflows" / "release.yml"
PI_PKG = REPO / "runtime" / "pi-host-adapter" / "package.json"
PI_LOCK = REPO / "runtime" / "pi-host-adapter" / "package-lock.json"

EXPECTED_MJS = [
    "adapter.mjs", "config.mjs", "executions.mjs", "extensions.mjs",
    "fingerprint.mjs", "logging.mjs", "login-path.mjs", "main.mjs",
    "paths.mjs", "policy.mjs", "release.mjs", "sdk-events.mjs",
    "sdk-transport.mjs", "server.mjs", "trusted-permission-extension.mjs",
    "wbrp.mjs",
]


def _load_yml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _workflow_on(data: dict) -> dict:
    # PyYAML parses unquoted `on:` as boolean True (YAML 1.1).
    if isinstance(data.get("on"), dict):
        return data["on"]
    if isinstance(data.get(True), dict):
        return data[True]
    return {}


def test_ci_baseline_current_and_least_privilege():
    data = _load_yml(CI_YML)
    text = CI_YML.read_text()
    assert data.get("permissions") == {"contents": "read"}
    assert "id-token: write" not in text
    # Current majors as of 2026-09-26.
    assert "actions/checkout@v7" in text
    assert "actions/setup-node@v7" in text
    assert "astral-sh/setup-uv@v10" in text
    assert "actions/setup-node@v6" not in text
    # Locked installs, no Playwright browsers.
    assert "--locked" in text
    assert "npm " in text and " ci" in text
    lowered = text.lower()
    assert "playwright" not in lowered
    assert "playwright install" not in lowered
    # Matrix covers Linux x64/arm64 + macOS arm64.
    assert "ubuntu-24.04" in text
    assert "ubuntu-24.04-arm" in text
    assert "macos-14" in text
    assert '"3.11"' in text or "'3.11'" in text or "3.11" in text
    assert '"3.13"' in text or "'3.13'" in text or "3.13" in text
    # Pi uses locked production deps + tests on all three OS.
    jobs = data.get("jobs", {})
    assert "python" in jobs and "manager" in jobs
    assert "pi" in jobs and "docker" in jobs
    pi_text = yaml.safe_dump(jobs["pi"])
    assert "ubuntu-24.04" in pi_text
    assert "ubuntu-24.04-arm" in pi_text
    assert "macos-14" in pi_text
    assert "--omit=dev" in text


def test_ci_artifact_action_majors():
    text = CI_YML.read_text()
    # CI baseline has no artifact upload/download; regression guard that
    # stale v4 majors never reappear if they are added later.
    assert "actions/upload-artifact@v4" not in text
    assert "actions/download-artifact@v4" not in text


def test_ci_docker_native_matrix():
    data = _load_yml(CI_YML)
    text = CI_YML.read_text()
    jobs = data.get("jobs", {})
    assert "docker" in jobs
    docker = jobs["docker"]
    dumped = yaml.safe_dump(docker)
    # Native matrix: ubuntu-24.04 for amd64, ubuntu-24.04-arm for arm64.
    assert "ubuntu-24.04" in dumped
    assert "ubuntu-24.04-arm" in dumped
    assert "amd64" in dumped
    assert "arm64" in dumped
    # Exact arch/os pairing.
    includes = docker.get("strategy", {}).get("matrix", {}).get(
        "include", [])
    pairing = {(row.get("os"), row.get("arch")) for row in includes}
    assert ("ubuntu-24.04", "amd64") in pairing
    assert ("ubuntu-24.04-arm", "arm64") in pairing
    # Same Dockerfile on both, arch inspected, bounded smoke, no push.
    assert "docker build" in dumped
    assert "Dockerfile" in text or "docker build" in text
    assert ".Architecture" in text
    assert "release" in dumped.lower() or "bridge_release" in text or \
        "read_manager_release" in text or "release-identity" in dumped
    assert "GHCR" not in text and "ghcr.io" not in text.lower()
    assert "packages: write" not in text


def test_release_artifact_action_majors_exact():
    text = RELEASE_YML.read_text()
    assert "actions/upload-artifact@v7" in text
    assert "actions/download-artifact@v8" in text
    assert "actions/upload-artifact@v4" not in text
    assert "actions/download-artifact@v4" not in text
    assert "actions/checkout@v7" in text
    assert "actions/setup-node@v7" in text
    assert "astral-sh/setup-uv@v10" in text


def test_release_triggers_and_permissions():
    data = _load_yml(RELEASE_YML)
    text = RELEASE_YML.read_text()
    on = _workflow_on(data)
    assert "pull_request" not in on
    assert "push" not in on
    assert "release" in on
    rel = on["release"]
    assert rel.get("types") == ["published"]
    assert data.get("permissions") == {"contents": "read"}
    assert "pull_request_target" not in text
    assert "workflow_run" not in text
    for bad in ("PYPI_TOKEN", "NPM_TOKEN", "NODE_AUTH_TOKEN"):
        assert bad not in text
    jobs = data.get("jobs", {})
    for required in ("pypi-publish", "npm-publish", "release-assets",
                     "release-ready", "prepare-pypi-dist", "build-docker"):
        assert required in jobs, required
    pypi = jobs["pypi-publish"]
    npm_pub = jobs["npm-publish"]
    assets = jobs["release-assets"]
    ready = jobs["release-ready"]
    prep = jobs["prepare-pypi-dist"]
    assert pypi.get("environment") == "pypi"
    assert npm_pub.get("environment") == "npm"
    assert pypi.get("permissions", {}).get("id-token") == "write"
    assert npm_pub.get("permissions", {}).get("id-token") == "write"
    assert assets.get("permissions", {}).get("contents") == "write"
    # Barrier and preparer never hold publishing identity.
    assert ready.get("permissions", {}).get("id-token") != "write"
    assert ready.get("permissions", {}).get("contents") != "write"
    assert prep.get("permissions", {}).get("id-token") != "write"
    assert prep.get("permissions", {}).get("contents", "read") != "write"
    for name, job in jobs.items():
        if name in ("pypi-publish", "npm-publish", "release-assets"):
            continue
        perms = job.get("permissions", {})
        assert perms.get("id-token") != "write", name
        assert perms.get("contents") != "write", name
    for job in jobs.values():
        for step in job.get("steps", []):
            uses = step.get("uses", "")
            assert "workflow_call" not in uses
    assert "packages: write" not in text
    assert "GHCR" not in text and "ghcr.io" not in text.lower()


def test_release_tag_consistency_gating():
    text = RELEASE_YML.read_text()
    assert "github.event.release.tag_name" in text
    assert "pyproject.toml" in text
    assert "workspaceBridgeRelease" in text
    assert "package.json" in text
    jobs = _load_yml(RELEASE_YML).get("jobs", {})
    assert "validate-release" in jobs
    for dep in ("build-source-bundle", "build-pi-runtime", "npm-pack",
                "build-docker", "prepare-pypi-dist"):
        needs = jobs[dep].get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert "validate-release" in needs, dep


def test_release_ready_barrier():
    data = _load_yml(RELEASE_YML)
    jobs = data.get("jobs", {})
    assert "release-ready" in jobs
    ready = jobs["release-ready"]
    needs = ready.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    for required in ("validate-release", "build-source-bundle",
                     "build-pi-runtime", "npm-pack", "build-docker",
                     "prepare-pypi-dist"):
        assert required in needs, required
    # Least privileges: read only, no OIDC/write.
    perms = ready.get("permissions", {})
    assert perms.get("contents") == "read" or "contents" not in perms
    assert perms.get("id-token") != "write"
    assert perms.get("contents") != "write"
    # All publishers and assets gate on release-ready.
    for name in ("pypi-publish", "npm-publish", "release-assets"):
        needs = jobs[name].get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert "release-ready" in needs, name


def test_release_docker_native_matrix_and_identity():
    data = _load_yml(RELEASE_YML)
    jobs = data.get("jobs", {})
    assert "build-docker" in jobs
    docker = jobs["build-docker"]
    dumped = yaml.safe_dump(docker)
    assert "ubuntu-24.04" in dumped
    assert "ubuntu-24.04-arm" in dumped
    assert "amd64" in dumped
    assert "arm64" in dumped
    includes = docker.get("strategy", {}).get("matrix", {}).get(
        "include", [])
    pairing = {(row.get("os"), row.get("arch")) for row in includes}
    assert ("ubuntu-24.04", "amd64") in pairing
    assert ("ubuntu-24.04-arm", "arm64") in pairing
    # Depends on validate + source bundle, validates bundle before build.
    needs = docker.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "validate-release" in needs
    assert "build-source-bundle" in needs
    assert "release validate" in dumped
    # Builds same Dockerfile, checks arch, verifies embedded identities
    # against the C1 target manifest. No GHCR push.
    assert "docker build" in dumped
    assert ".Architecture" in dumped
    assert "target-manifest" in dumped
    assert "container" in dumped.lower() or "Bridge" in dumped
    full = RELEASE_YML.read_text()
    docker_section = full.split("build-docker", 1)[1].split(
        "npm-pack", 1)[0]
    assert "ghcr" not in docker_section.lower()
    assert "docker push" not in docker_section.lower()
    assert "packages: write" not in full


def test_pypi_oidc_isolation():
    data = _load_yml(RELEASE_YML)
    jobs = data.get("jobs", {})
    assert "prepare-pypi-dist" in jobs and "pypi-publish" in jobs
    prep = jobs["prepare-pypi-dist"]
    pub = jobs["pypi-publish"]
    # Preparer has no OIDC; publisher is minimal.
    assert prep.get("permissions", {}).get("id-token") != "write"
    assert pub.get("environment") == "pypi"
    assert pub.get("permissions", {}).get("id-token") == "write"
    assert pub.get("permissions", {}).get("contents") == "read"
    pub_steps = pub.get("steps", [])
    # ONLY download + official publish action.
    assert len(pub_steps) == 2, yaml.safe_dump(pub_steps)
    assert "download-artifact" in str(pub_steps[0])
    assert "pypi-dist" in str(pub_steps[0])
    assert "pypa/gh-action-pypi-publish" in str(pub_steps[1])
    pub_dump = yaml.safe_dump(pub)
    assert "actions/checkout" not in pub_dump
    assert "setup-uv" not in pub_dump
    assert "uv sync" not in pub_dump
    assert "uv run" not in pub_dump
    assert "validate-bundle" not in pub_dump
    assert "release validate" not in pub_dump
    assert "python3" not in pub_dump.lower() or "publish" in pub_dump.lower()
    # Publisher depends on barrier + prepared dist.
    needs = pub.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "release-ready" in needs
    assert "prepare-pypi-dist" in needs
    # Preparer does validation + wheel SHA staging, uploads pypi-dist.
    prep_dump = yaml.safe_dump(prep)
    assert "release validate" in prep_dump
    assert "sha256" in RELEASE_YML.read_text().lower()
    assert "pypi-dist" in prep_dump
    assert "upload-artifact" in prep_dump


def test_pypi_uses_exact_build_artifact():
    text = RELEASE_YML.read_text()
    assert "release validate" in text
    assert "sha256" in text.lower() or "SHA" in text
    assert "pypa/gh-action-pypi-publish" in text
    # Publish job itself never rebuilds.
    jobs = _load_yml(RELEASE_YML).get("jobs", {})
    pub_dump = yaml.safe_dump(jobs["pypi-publish"])
    assert "uv build" not in pub_dump
    assert "deploy build" not in pub_dump


def test_npm_oidc_isolation_and_version_gate():
    data = _load_yml(RELEASE_YML)
    jobs = data.get("jobs", {})
    npm_pub = jobs["npm-publish"]
    assert npm_pub.get("if") is not None
    assert "NPM_TRUSTED_PUBLISHING_ENABLED" in str(npm_pub.get("if"))
    needs = npm_pub.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "release-ready" in needs
    assert "npm-pack" in needs
    steps = npm_pub.get("steps", [])
    # No checkout/build/test/pack in OIDC job.
    dumped = yaml.safe_dump(npm_pub)
    assert "actions/checkout" not in dumped
    assert "npm --prefix" not in dumped
    assert "npm pack" not in "\n".join(
        str(s.get("run", "")) for s in steps)
    assert "npm test" not in "\n".join(
        str(s.get("run", "")) for s in steps)
    # setup-node without cache, with registry-url allowed.
    setup = [s for s in steps if "setup-node" in str(s.get("uses", ""))]
    assert len(setup) == 1
    assert "registry-url" in yaml.safe_dump(setup[0])
    assert "cache:" not in yaml.safe_dump(setup[0])
    # Bounded npm version check >=11.5.1, no install/upgrade.
    runs = "\n".join(str(s.get("run", "")) for s in steps)
    assert "11.5.1" in runs
    assert "npm --version" in runs
    assert "npm install -g" not in runs
    assert "npm i -g" not in runs
    # Exact tgz, staged by default, direct only via var.
    assert "download-artifact" in dumped
    assert "npm-package" in dumped
    assert "*.tgz" in RELEASE_YML.read_text()
    assert "npm stage publish" in RELEASE_YML.read_text()
    assert "NPM_PUBLISH_MODE" in RELEASE_YML.read_text()
    assert "NPM_TOKEN" not in RELEASE_YML.read_text()
    assert "--provenance=false" in RELEASE_YML.read_text()
    pack = jobs["npm-pack"]
    assert pack.get("if") is None or "NPM_TRUSTED_PUBLISHING_ENABLED" not in str(
        pack.get("if", ""))
    assert pack.get("permissions", {}).get("id-token") != "write"


def test_pi_validates_before_extraction():
    text = RELEASE_YML.read_text()
    jobs = _load_yml(RELEASE_YML).get("jobs", {})
    pi = jobs["build-pi-runtime"]
    steps = pi.get("steps", [])
    runs = [str(s.get("run", "")) + str(s.get("uses", "")) for s in steps]
    validate_idx = next(
        i for i, r in enumerate(runs) if "release validate" in r)
    extract_idx = next(i for i, r in enumerate(runs) if "tar -xzf" in r)
    assert validate_idx < extract_idx
    assert "download-artifact" in yaml.safe_dump(pi)
    assert "source-bundle" in yaml.safe_dump(pi)


def test_runner_matrix_covers_three_platforms():
    data = _load_yml(RELEASE_YML)
    jobs = data.get("jobs", {})
    assert "build-pi-runtime" in jobs
    dumped = yaml.safe_dump(jobs["build-pi-runtime"])
    assert "ubuntu-24.04" in dumped
    assert "ubuntu-24.04-arm" in dumped
    assert "macos-14" in dumped
    assert "linux" in dumped and "darwin" in dumped
    assert "x64" in dumped and "arm64" in dumped
    source = jobs["build-source-bundle"]
    assert source.get("runs-on") == "ubuntu-24.04"


def test_release_cannot_run_on_pr():
    for path in (CI_YML, RELEASE_YML):
        data = _load_yml(path)
        if path == RELEASE_YML:
            assert "pull_request" not in _workflow_on(data)
        if path == CI_YML:
            text = path.read_text()
            assert "pypa/gh-action-pypi-publish" not in text
            assert "npm publish" not in text
            assert "npm stage publish" not in text


def test_pi_package_publish_inventory():
    pkg = json.loads(PI_PKG.read_text())
    assert pkg.get("private") is not True
    assert pkg.get("name") == "workspace-bridge-pi-host-adapter"
    repo = pkg.get("repository", {})
    assert repo.get("url") == "https://github.com/sheldonxxxx/workspace-bridge"
    assert repo.get("directory") == "runtime/pi-host-adapter"
    pub = pkg.get("publishConfig", {})
    assert pub.get("access") == "public"
    files = pkg.get("files", [])
    assert sorted(files) == sorted(EXPECTED_MJS)
    for name in EXPECTED_MJS:
        assert (REPO / "runtime" / "pi-host-adapter" / name).is_file()
    assert "test" not in " ".join(files).lower() or all(
        not f.startswith("test/") for f in files)
    assert not any(f.startswith("launchd/") for f in files)
    assert not any("node_modules" in f for f in files)
    assert not any(f == "README.md" for f in files)


def test_npm_pack_inventory_matches_allowlist():
    result = subprocess.run(
        ["npm", "pack", "--dry-run", "--json"],
        cwd=str(REPO / "runtime" / "pi-host-adapter"),
        capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert isinstance(data, list) and len(data) == 1
    paths = sorted(f["path"] for f in data[0]["files"])
    for name in EXPECTED_MJS:
        assert name in paths, name
    assert "package.json" in paths
    assert not any(p.startswith("test/") for p in paths)
    assert not any(p.startswith("launchd/") for p in paths)
    assert not any("node_modules" in p for p in paths)
    assert not any("token" in p.lower() for p in paths)


def test_release_assets_use_gh_upload_no_new_tag():
    text = RELEASE_YML.read_text()
    jobs = _load_yml(RELEASE_YML).get("jobs", {})
    assets = jobs["release-assets"]
    assert "gh release upload" in text
    assert "GITHUB_TOKEN" in text or "github.token" in text
    assert "gh release create" not in text
    assert "--clobber" in text or "clobber" in text
    for needle in ("bundle.json", "target-manifest", "python-wheel",
                   "pi-host-adapter", "pi-runtime"):
        assert needle in text
    assert "${VERSION}" in text or "$VERSION" in text or "VERSION" in text
    needs = assets.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "release-ready" in needs
    assert "build-source-bundle" in needs
    assert "build-pi-runtime" in needs
    assert assets.get("permissions", {}).get("id-token") != "write"
    assert "ghcr" not in text.lower()
    assert ".tar\"" not in text or "pi-runtime" in text


def test_release_workflow_uses_release_cli_and_packaging_smoke():
    text = RELEASE_YML.read_text()
    # Old deploy CLI names are gone from release automation.
    assert "workspace-bridge deploy" not in text
    assert "deploy select-plan" not in text
    assert "deploy backup" not in text
    assert "deploy plan" not in text
    # Release-engineering names are used for bundle build/validation.
    assert "workspace-bridge release build" in text
    assert "workspace-bridge release validate" in text
    # Packaging smoke: wheel console entry metadata plus version and
    # nested node help, with no network install step.
    assert "entry_points.txt" in text
    assert "workspace_bridge.cli:main" in text
    assert "workspace_bridge.node_cli:main" in text
    assert "workspace-bridge --version" in text
    assert "workspace-bridge node --help" in text
