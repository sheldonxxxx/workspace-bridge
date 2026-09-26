"""M4.2C1 deterministic release bundle: construction, validation, CLI."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from workspace_bridge import __version__
from workspace_bridge.release_manifest import make_manifest, validate_manifest
from workspace_bridge.release import validate_release
from workspace_bridge.release_bundle import (
    _MANAGER_JS,
    _PI_INPUTS_JS,
    _PI_JS,
    BundleError,
    _core_build_id_for_dir,
    _create_pi_archive,
    _normalize_wheel,
    _safe_extract_tar,
    _safe_extract_zip,
    assemble_bundle,
    build_bundle,
    compute_bundle_id,
    list_pi_inputs,
    validate_release_bundle,
)


def _synthetic(component: str, build: str, product: str | None = None) -> dict:
    return validate_release({
        "contract": 1, "product": "workspace-bridge",
        "product_version": product or __version__, "component": component,
        "component_version": "0.1.0", "build_id": build,
    })


FAKE_MANAGER = _synthetic("manager", "sha256:" + "e" * 64)
FAKE_PI = _synthetic("pi-host-adapter", "sha256:" + "f" * 64)
FAKE_PI_INPUTS = ["adapter.mjs", "package-lock.json", "package.json"]


class _FakeResult:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def _fake_run_factory(*, pi_inputs=None, pi_release=None, manager_release=None):
    pi_inputs = pi_inputs if pi_inputs is not None else FAKE_PI_INPUTS
    pi_release = pi_release if pi_release is not None else FAKE_PI
    manager_release = manager_release if manager_release is not None else FAKE_MANAGER

    def _fake(argv, **kwargs):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        assert kwargs.get("shell") is not True
        # Fixed typed argv: [node, --input-type=module, -e, script, module, root]
        assert argv[1] == "--input-type=module"
        assert argv[2] == "-e"
        script = argv[3]
        if script == _PI_INPUTS_JS:
            return _FakeResult(json.dumps(pi_inputs))
        if script == _PI_JS:
            return _FakeResult(json.dumps(pi_release))
        if script == _MANAGER_JS:
            return _FakeResult(json.dumps(manager_release))
        raise AssertionError(f"unexpected helper script: {script!r}")
    return _fake


def _make_package_dir(base: Path, *, manager_release: dict = FAKE_MANAGER) -> Path:
    pkg = base / "workspace_bridge"
    (pkg / "skills" / "project-lead").mkdir(parents=True, exist_ok=True)
    (pkg / "static" / "dist").mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("__version__ = \"0.1.0\"\n")
    (pkg / "foo.py").write_text("VALUE = 1\n")
    (pkg / "skills" / "project-lead" / "SKILL.md").write_text("# Skill\n")
    (pkg / "static" / "dist" / "release.json").write_text(
        json.dumps(manager_release, sort_keys=True, indent=2))
    return pkg


def _manifest_for_package(pkg: Path, *, manager_release: dict = FAKE_MANAGER,
                           pi_release: dict = FAKE_PI) -> dict:
    core = _core_build_id_for_dir(pkg)
    return make_manifest(
        product_version=__version__,
        bridge=_synthetic("bridge", core),
        manager=manager_release,
        node=_synthetic("node", core),
        codex=_synthetic("codex-host-adapter", core),
        pi=pi_release,
    )


def _wheel_from_package(pkg: Path, dest: Path) -> None:
    raw = dest.parent / (dest.name + ".raw.whl")
    with zipfile.ZipFile(raw, "w") as z:
        for path in sorted(pkg.rglob("*")):
            if not path.is_file():
                continue
            rel = "workspace_bridge/" + path.relative_to(pkg).as_posix()
            z.writestr(rel, path.read_bytes())
        # Minimal dist-info so the wheel looks real (ignored by validation).
        z.writestr("workspace_bridge-0.1.0.dist-info/METADATA",
                   "Metadata-Version: 2.1\nName: workspace-bridge\n")
    _normalize_wheel(src=raw, dest=dest)
    raw.unlink()


def _pi_src_dir(base: Path) -> Path:
    src = base / "pi-src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "adapter.mjs").write_text("export const x = 1;\n")
    (src / "package.json").write_text('{"name":"pi","version":"0.1.0"}\n')
    (src / "package-lock.json").write_text('{"lockfileVersion":3}\n')
    return src


def _build_fixture_bundle(output: Path, *, manifest=None) -> dict:
    """Build a valid fixture bundle with injectable fakes (no network)."""
    work = output.parent / (output.name + ".work")
    work.mkdir(parents=True, exist_ok=True)
    pkg_base = work / "pkgbase"
    pkg_base.mkdir(exist_ok=True)
    pkg = _make_package_dir(pkg_base)
    manifest = manifest if manifest is not None else _manifest_for_package(pkg)
    wheel_tmp = work / "wheel.whl"
    _wheel_from_package(pkg, wheel_tmp)
    pi_src = _pi_src_dir(work)
    pi_tmp = work / "pi.tar.gz"
    _create_pi_archive(src_root=pi_src, names=FAKE_PI_INPUTS, dest=pi_tmp)
    fake = _fake_run_factory()
    return assemble_bundle(output=output, manifest=manifest,
                           wheel_path=wheel_tmp, pi_archive_path=pi_tmp,
                           _run=fake)


def test_deterministic_repeated_builds_identical(tmp_path):
    out1 = tmp_path / "bundle1"
    out2 = tmp_path / "bundle2"
    fake = _fake_run_factory()
    # Identical staged fixtures: same package + pi src produce identical hashes.
    work = tmp_path / "work"
    work.mkdir()
    pkg = _make_package_dir(work / "pkg")
    manifest = _manifest_for_package(pkg)
    wheel_a = work / "a.whl"
    wheel_b = work / "b.whl"
    _wheel_from_package(pkg, wheel_a)
    _wheel_from_package(pkg, wheel_b)
    assert wheel_a.read_bytes() == wheel_b.read_bytes()
    pi_src = _pi_src_dir(work / "piwork")
    pi_a = work / "a.tar.gz"
    pi_b = work / "b.tar.gz"
    _create_pi_archive(src_root=pi_src, names=FAKE_PI_INPUTS, dest=pi_a)
    _create_pi_archive(src_root=pi_src, names=FAKE_PI_INPUTS, dest=pi_b)
    assert pi_a.read_bytes() == pi_b.read_bytes()
    r1 = assemble_bundle(output=out1, manifest=manifest,
                         wheel_path=wheel_a, pi_archive_path=pi_a, _run=fake)
    r2 = assemble_bundle(output=out2, manifest=manifest,
                         wheel_path=wheel_b, pi_archive_path=pi_b, _run=fake)
    assert r1["bundle"] == r2["bundle"]
    assert r1["manifest"] == r2["manifest"]
    assert r1["bundle"]["bundle_id"] == r2["bundle"]["bundle_id"]
    # Artifact hashes identical.
    assert [e["sha256"] for e in r1["bundle"]["artifacts"]] == [
        e["sha256"] for e in r2["bundle"]["artifacts"]]


def test_build_bundle_deterministic_with_injection(tmp_path):
    work = tmp_path / "stage"
    work.mkdir()
    pkg = _make_package_dir(work / "pkg")
    manifest = _manifest_for_package(pkg)
    wheel_file = work / "fixture.whl"
    _wheel_from_package(pkg, wheel_file)
    pi_src = _pi_src_dir(work / "pi")
    pi_file = work / "fixture.tar.gz"
    _create_pi_archive(src_root=pi_src, names=FAKE_PI_INPUTS, dest=pi_file)
    fake = _fake_run_factory()

    def _wheel_build(out_dir: Path):
        dest = out_dir / "built.whl"
        import shutil as _sh
        _sh.copyfile(wheel_file, dest)
        return dest

    def _pi_build(tmpdir: Path):
        dest = tmpdir / "pi.tar.gz"
        import shutil as _sh
        _sh.copyfile(pi_file, dest)
        return dest

    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    # Bypass the real Manager build + release.json check by injecting a
    # manifest fn and a no-op manager build, then patch the release.json
    # check via matching real file? Instead use assemble-level determinism
    # through build_bundle with a manifest that matches the REAL served
    # manager? For fixture determinism we bypass via _manifest_fn + manager
    # no-op and monkeypatch the served check to our fixture manager.
    # Simplest: call build_bundle with injections that produce fixture
    # artifacts and a fixture manifest, and temporarily point the served
    # release.json check at our fixture by monkeypatching.
    import workspace_bridge.release_bundle as rb
    real_repo_root = rb.REPO_ROOT
    # Create a fake repo root containing our fixture release.json.
    fake_repo = tmp_path / "fakerepo"
    (fake_repo / "workspace_bridge" / "static" / "dist").mkdir(parents=True)
    (fake_repo / "workspace_bridge" / "static" / "dist" / "release.json").write_text(
        json.dumps(FAKE_MANAGER, sort_keys=True, indent=2))
    rb.REPO_ROOT = fake_repo
    try:
        r1 = build_bundle(output=out1,
                          _manager_build=lambda: None,
                          _wheel_build=_wheel_build,
                          _pi_build=_pi_build,
                          _manifest_fn=lambda: manifest,
                          _run=fake)
        r2 = build_bundle(output=out2,
                          _manager_build=lambda: None,
                          _wheel_build=_wheel_build,
                          _pi_build=_pi_build,
                          _manifest_fn=lambda: manifest,
                          _run=fake)
    finally:
        rb.REPO_ROOT = real_repo_root
    assert r1["bundle"] == r2["bundle"]
    assert r1["bundle"]["bundle_id"] == r2["bundle"]["bundle_id"]


def test_mutation_of_wheel_fails(tmp_path):
    out = tmp_path / "bundle"
    _build_fixture_bundle(out)
    fake = _fake_run_factory()
    wheel = out / "artifacts" / "python-wheel.whl"
    data = bytearray(wheel.read_bytes())
    data[len(data) // 2] ^= 1
    wheel.write_bytes(bytes(data))
    with pytest.raises(BundleError):
        validate_release_bundle(out, _run=fake)


def test_mutation_of_pi_archive_fails(tmp_path):
    out = tmp_path / "bundle"
    _build_fixture_bundle(out)
    fake = _fake_run_factory()
    pi = out / "artifacts" / "pi-host-adapter.tar.gz"
    data = bytearray(pi.read_bytes())
    data[len(data) // 2] ^= 1
    pi.write_bytes(bytes(data))
    with pytest.raises(BundleError):
        validate_release_bundle(out, _run=fake)


def test_mutation_of_manifest_fails(tmp_path):
    out = tmp_path / "bundle"
    result = _build_fixture_bundle(out)
    fake = _fake_run_factory()
    manifest_path = out / "target-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    # Flip one hex char in the Pi build ID (keeps shape, breaks binding).
    pi_build = manifest["components"]["pi-host-adapter"]["build_id"]
    flipped = pi_build[:-1] + ("0" if pi_build[-1] != "0" else "1")
    manifest["components"]["pi-host-adapter"]["build_id"] = flipped
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2))
    with pytest.raises(BundleError):
        validate_release_bundle(out, _run=fake)
    assert result["bundle"]["manifest_id"] != flipped


def test_mutation_of_bundle_metadata_fails(tmp_path):
    out = tmp_path / "bundle"
    _build_fixture_bundle(out)
    fake = _fake_run_factory()
    bundle_path = out / "bundle.json"
    bundle = json.loads(bundle_path.read_text())
    bundle["artifacts"][0]["size"] += 1
    bundle_path.write_text(json.dumps(bundle, sort_keys=True, indent=2))
    with pytest.raises(BundleError):
        validate_release_bundle(out, _run=fake)


def test_unexpected_files_fail(tmp_path):
    out = tmp_path / "bundle"
    _build_fixture_bundle(out)
    fake = _fake_run_factory()
    (out / "extra.txt").write_text("unexpected")
    with pytest.raises(BundleError):
        validate_release_bundle(out, _run=fake)
    (out / "extra.txt").unlink()
    (out / "artifacts" / "extra.whl").write_text("unexpected")
    with pytest.raises(BundleError):
        validate_release_bundle(out, _run=fake)


def test_symlink_in_bundle_fails(tmp_path):
    out = tmp_path / "bundle"
    _build_fixture_bundle(out)
    fake = _fake_run_factory()
    link = out / "artifacts" / "python-wheel.whl"
    target = tmp_path / "outside.whl"
    target.write_bytes(b"outside")
    raw = link.read_bytes()
    link.unlink()
    link.symlink_to(target)
    try:
        with pytest.raises(BundleError):
            validate_release_bundle(out, _run=fake)
    finally:
        link.unlink()
        link.write_bytes(raw)


def test_symlinked_bundle_dir_rejected(tmp_path):
    out = tmp_path / "bundle"
    _build_fixture_bundle(out)
    fake = _fake_run_factory()
    link = tmp_path / "linkbundle"
    link.symlink_to(out, target_is_directory=True)
    with pytest.raises(BundleError):
        validate_release_bundle(link, _run=fake)


def test_wheel_path_traversal_rejected(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    evil = work / "evil.whl"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../../evil.py", "evil\n")
        z.writestr("workspace_bridge/__init__.py", "x\n")
    dest = work / "out"
    dest.mkdir()
    with pytest.raises(BundleError):
        _safe_extract_zip(src=evil, dest=dest)


def test_wheel_symlink_rejected(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    evil = work / "evil.whl"
    info = zipfile.ZipInfo("workspace_bridge/link.py")
    info.create_system = 3
    info.external_attr = (0o120777 << 16)
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr(info, "target\n")
        z.writestr("workspace_bridge/__init__.py", "x\n")
    dest = work / "out"
    dest.mkdir()
    with pytest.raises(BundleError):
        _safe_extract_zip(src=evil, dest=dest)


def test_pi_path_traversal_rejected(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    evil = work / "evil.tar.gz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT,
                       pax_headers={}) as tar:
        info = tarfile.TarInfo("../evil.mjs")
        data = b"evil\n"
        info.size = len(data)
        info.mtime = 0
        info.mode = 0o644
        info.type = tarfile.REGTYPE
        tar.addfile(info, io.BytesIO(data))
    with open(evil, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", compresslevel=9,
                           mtime=0, fileobj=raw) as gz:
            gz.write(buf.getvalue())
    dest = work / "out"
    dest.mkdir()
    with pytest.raises(BundleError):
        _safe_extract_tar(src=evil, dest=dest)


def test_pi_symlink_rejected(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    evil = work / "evil.tar.gz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT,
                       pax_headers={}) as tar:
        info = tarfile.TarInfo("link.mjs")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        info.mtime = 0
        tar.addfile(info)
    with open(evil, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", compresslevel=9,
                           mtime=0, fileobj=raw) as gz:
            gz.write(buf.getvalue())
    dest = work / "out"
    dest.mkdir()
    with pytest.raises(BundleError):
        _safe_extract_tar(src=evil, dest=dest)


def test_wrong_embedded_manager_fails(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    pkg = _make_package_dir(work / "pkg", manager_release=_synthetic(
        "manager", "sha256:" + "a" * 64))
    manifest = _manifest_for_package(work / "pkg")  # expects e*64, wheel has a*64
    wheel = work / "wheel.whl"
    _wheel_from_package(pkg, wheel)
    pi_src = _pi_src_dir(work / "pi")
    pi_arch = work / "pi.tar.gz"
    _create_pi_archive(src_root=pi_src, names=FAKE_PI_INPUTS, dest=pi_arch)
    out = tmp_path / "bundle"
    out.mkdir()
    # Assemble must fail closed on manager mismatch (direct validator).
    from workspace_bridge.release_bundle import _validate_wheel_against_manifest
    with pytest.raises(BundleError):
        _validate_wheel_against_manifest(wheel_path=wheel, manifest=manifest)


def test_wrong_core_build_fails(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    pkg = _make_package_dir(work / "pkg")
    (pkg / "foo.py").write_text("VALUE = 2\n")  # change after manifest
    manifest = _manifest_for_package(_make_package_dir(work / "pkg2"))
    # Wheel from pkg (VALUE=2) vs manifest from pkg2 (VALUE=1) -> mismatch.
    wheel = work / "wheel.whl"
    _wheel_from_package(pkg, wheel)
    from workspace_bridge.release_bundle import _validate_wheel_against_manifest
    with pytest.raises(BundleError):
        _validate_wheel_against_manifest(wheel_path=wheel, manifest=manifest)


def test_wrong_pi_identity_fails(tmp_path):
    out = tmp_path / "bundle"
    _build_fixture_bundle(out)
    wrong_pi = _synthetic("pi-host-adapter", "sha256:" + "0" * 64)
    fake_wrong = _fake_run_factory(pi_release=wrong_pi)
    with pytest.raises(BundleError):
        validate_release_bundle(out, _run=fake_wrong)


def test_pi_inventory_mismatch_fails(tmp_path):
    out = tmp_path / "bundle"
    _build_fixture_bundle(out)
    # Validator sees different inventory than archived -> fail.
    fake_extra = _fake_run_factory(
        pi_inputs=["adapter.mjs", "extra.mjs", "package-lock.json", "package.json"])
    with pytest.raises(BundleError):
        validate_release_bundle(out, _run=fake_extra)


def test_output_overwrite_fails(tmp_path):
    out = tmp_path / "bundle"
    _build_fixture_bundle(out)
    fake = _fake_run_factory()
    before = (out / "bundle.json").read_bytes()
    with pytest.raises(BundleError) as exc:
        _build_fixture_bundle(out)
    assert exc.value.code in ("bundle-exists", "bundle_failed",
                              "bundle-write-failed", "bundle-unavailable")
    assert (out / "bundle.json").read_bytes() == before
    # Non-empty non-bundle dir also refused.
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "random.txt").write_text("x")
    with pytest.raises(BundleError):
        _build_fixture_bundle(dirty)


def test_output_symlink_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linkout"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(BundleError):
        _build_fixture_bundle(link)


def test_safe_metadata_has_no_paths_secrets_timestamps(tmp_path):
    out = tmp_path / "bundle"
    result = _build_fixture_bundle(out)
    bundle = result["bundle"]
    manifest = result["manifest"]
    text = json.dumps(bundle, sort_keys=True, ensure_ascii=False)
    lowered = text.lower()
    assert "/tmp/" not in text
    assert "/volumes/" not in text.lower()
    assert "/home/" not in text
    assert "http://" not in text
    assert "https://" not in text
    assert "token" not in lowered
    assert "secret" not in lowered
    assert "timestamp" not in lowered
    assert "created" not in lowered
    # No absolute paths except allowlisted bundle-relative logicals.
    for entry in bundle["artifacts"]:
        assert entry["logical"] in ("artifacts/python-wheel.whl",
                                    "artifacts/pi-host-adapter.tar.gz")
        assert not entry["logical"].startswith("/")
    # Bundle ID is content-addressed over canonical metadata.
    recomputed = compute_bundle_id(
        product_version=bundle["product_version"],
        manifest_id=bundle["manifest_id"],
        artifacts=[{k: e[k] for k in ("logical", "kind", "sha256", "size",
                                      "components", "releases")}
                   for e in bundle["artifacts"]])
    assert recomputed == bundle["bundle_id"]
    assert bundle["receipt_id"] == bundle["bundle_id"]
    assert bundle["product_version"] == manifest["product_version"]
    assert bundle["manifest_id"] == manifest["manifest_id"]


def test_typed_subprocess_argv_has_no_shell(tmp_path):
    import ast as _ast
    import pathlib as _pl
    import workspace_bridge.release_bundle as rb
    tree = _ast.parse(_pl.Path(rb.__file__).read_text())
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            for kw in node.keywords:
                if kw.arg == "shell":
                    # Only shell=False (or falsy constant) is allowed.
                    assert isinstance(kw.value, _ast.Constant) and not kw.value.value, \
                        "release_bundle must never use shell=True"
    # Fixed helpers never accept caller command text: signatures have no cmd.
    import inspect as _in
    assert "shell" not in _in.signature(rb._run_node_helper).parameters
    assert "cmd" not in _in.signature(rb.run_manager_build).parameters
    assert "cmd" not in _in.signature(rb.run_wheel_build).parameters
    # Recorded argv is a typed list with fixed structure.
    recorded: list = []

    def _rec(argv, **kwargs):
        recorded.append((argv, kwargs))
        assert isinstance(argv, list)
        assert all(isinstance(a, str) for a in argv)
        assert kwargs.get("shell") is not True
        return _FakeResult(json.dumps(FAKE_PI_INPUTS))

    list_pi_inputs(_run=_rec)
    assert recorded
    argv, kwargs = recorded[0]
    assert argv[1] == "--input-type=module"
    assert "--" not in argv[0]

    recorded_manager: list = []

    def _rec_manager(argv, **kwargs):
        recorded_manager.append((argv, kwargs))
        assert isinstance(argv, list)
        assert kwargs.get("shell") is not True
        return _FakeResult("", returncode=0)

    from workspace_bridge.release_bundle import run_manager_build, run_wheel_build
    run_manager_build(_run=_rec_manager)
    assert recorded_manager[0][0][:2] == [recorded_manager[0][0][0], "run"]
    assert recorded_manager[0][0][1] == "run"


def test_component_coverage_is_strict(tmp_path):
    out = tmp_path / "bundle"
    result = _build_fixture_bundle(out)
    bundle = result["bundle"]
    by_logical = {e["logical"]: e for e in bundle["artifacts"]}
    assert by_logical["artifacts/python-wheel.whl"]["components"] == [
        "bridge", "codex-host-adapter", "manager", "node"]
    assert by_logical["artifacts/pi-host-adapter.tar.gz"]["components"] == [
        "pi-host-adapter"]
    covered = sorted(c for e in bundle["artifacts"] for c in e["components"])
    assert covered == ["bridge", "codex-host-adapter", "manager", "node",
                       "pi-host-adapter"]
    # Tampered coverage fails.
    tampered = json.loads((out / "bundle.json").read_text())
    tampered["artifacts"][0]["components"] = ["pi-host-adapter", "bridge"]
    tampered["artifacts"][0]["components"].sort()
    (out / "bundle.json").write_text(json.dumps(tampered, sort_keys=True, indent=2))
    with pytest.raises(BundleError):
        validate_release_bundle(out, _run=_fake_run_factory())


def test_bundle_layout_is_fixed_allowlist(tmp_path):
    out = tmp_path / "bundle"
    _build_fixture_bundle(out)
    assert sorted(p.name for p in out.iterdir()) == [
        "artifacts", "bundle.json", "target-manifest.json"]
    assert sorted(p.name for p in (out / "artifacts").iterdir()) == [
        "pi-host-adapter.tar.gz", "python-wheel.whl"]
    for path in [out / "bundle.json", out / "target-manifest.json",
                 out / "artifacts" / "python-wheel.whl",
                 out / "artifacts" / "pi-host-adapter.tar.gz"]:
        assert not path.is_symlink()
        assert path.is_file()


def test_cli_build_and_validate_statuses(tmp_path, capsys, monkeypatch):
    from workspace_bridge.cli import main as cli_main
    import workspace_bridge.release_bundle as rb

    # Hermetic CLI: patch heavy builders to fixture artifacts with REAL
    # manager identity so the served release.json check passes.
    from workspace_bridge.release import read_manager_release
    real_manager = read_manager_release()
    assert real_manager is not None
    work = tmp_path / "stage"
    work.mkdir()
    pkg_base = work / "pkgbase"
    pkg_base.mkdir()
    # Fixture package must produce a core that we then embed in the manifest,
    # but the wheel must be built from the REAL source to match the real
    # core? Instead: build the manifest from the REAL core + REAL manager,
    # and build the wheel from the REAL source files (copy real package).
    # Simpler: monkeypatch build_bundle to a fixture that uses real identities
    # by copying real files into a temp wheel.
    def _fake_build_bundle(*, output, **kw):
        # Use real manifest (bridge/node/codex/manager/pi from source).
        import sys as _sys
        import subprocess as _sp
        import shutil as _sh
        # Real manifest via real helpers (node available in CI).
        manifest = rb.build_target_manifest()
        # Build a wheel zip from the REAL installed package dir.
        from workspace_bridge.release import _iter_core_inputs
        from pathlib import Path as _P
        real_pkg = _P(rb.__file__).resolve().parent
        # Create deterministic wheel containing real package files.
        tmp_wheel = work / "real.whl"
        raw = work / "real.raw.whl"
        with zipfile.ZipFile(raw, "w") as z:
            for rel, path in _iter_core_inputs(real_pkg):
                z.writestr("workspace_bridge/" + rel, path.read_bytes())
            # Packaged manager release.json (real).
            rel_json = real_pkg / "static" / "dist" / "release.json"
            z.writestr("workspace_bridge/static/dist/release.json",
                       rel_json.read_bytes())
            z.writestr("workspace_bridge-0.1.0.dist-info/METADATA",
                       "Metadata-Version: 2.1\nName: workspace-bridge\n")
        rb._normalize_wheel(src=raw, dest=tmp_wheel)
        raw.unlink()
        # Real Pi archive from the real PI_ROOT.
        pi_names = rb.list_pi_inputs()
        tmp_pi = work / "real.tar.gz"
        rb._create_pi_archive(src_root=rb.PI_ROOT, names=pi_names, dest=tmp_pi)
        return rb.assemble_bundle(output=Path(output), manifest=manifest,
                                  wheel_path=tmp_wheel,
                                  pi_archive_path=tmp_pi)
    monkeypatch.setattr(rb, "build_bundle", _fake_build_bundle)
    out = tmp_path / "cli-bundle"
    assert cli_main(["--state", str(tmp_path / "unused-state"),
                     "release", "build", "--output", str(out), "--json"]) is None
    data = json.loads(capsys.readouterr().out)
    assert data["bundle_id"].startswith("sha256:")
    assert data["manifest_id"].startswith("sha256:")
    assert len(data["artifacts"]) == 2
    # Human mode on a second bundle.
    out2 = tmp_path / "cli-bundle2"
    assert cli_main(["--state", str(tmp_path / "unused-state"),
                     "release", "build", "--output", str(out2)]) is None
    human = capsys.readouterr().out
    assert "Release bundle: COMPLETE" in human
    assert "token" not in human.lower()
    assert "http://" not in human.lower()
    # Release-validate success (both JSON + human).
    assert cli_main(["--state", str(tmp_path / "unused-state"),
                     "release", "validate", "--bundle", str(out),
                     "--json"]) is None
    validated = json.loads(capsys.readouterr().out)
    assert validated["bundle_id"] == data["bundle_id"]
    assert cli_main(["--state", str(tmp_path / "unused-state"),
                     "release", "validate", "--bundle", str(out)]) is None
    assert "Release bundle: COMPLETE" in capsys.readouterr().out
    # Overwrite fails closed with exit 1 and preserves bytes.
    before = (out / "bundle.json").read_bytes()
    with pytest.raises(SystemExit) as exc:
        cli_main(["--state", str(tmp_path / "unused-state"),
                  "release", "build", "--output", str(out), "--json"])
    assert exc.value.code == 1
    assert (out / "bundle.json").read_bytes() == before
    capsys.readouterr()  # consume overwrite-failure JSON
    # Corrupt bundle fails validate with exit 1.
    (out / "bundle.json").write_text(
        (out / "bundle.json").read_text().replace("sha256:", "sha256:0", 1))
    with pytest.raises(SystemExit) as exc2:
        cli_main(["--state", str(tmp_path / "unused-state"),
                  "release", "validate", "--bundle", str(out),
                  "--json"])
    assert exc2.value.code == 1
    err = json.loads(capsys.readouterr().out)
    assert err["status"] == "failed"


def test_cli_build_does_not_require_state(tmp_path, capsys, monkeypatch):
    from workspace_bridge.cli import main as cli_main
    import workspace_bridge.release_bundle as rb
    missing_state = tmp_path / "no-such-state"
    assert not missing_state.exists()
    out = tmp_path / "nostate-bundle"
    # Reuse the hermetic fake from the previous test via fresh patch.
    work = tmp_path / "stage2"
    work.mkdir()

    def _fake_build_bundle(*, output, **kw):
        manifest = rb.build_target_manifest()
        from workspace_bridge.release import _iter_core_inputs
        from pathlib import Path as _P
        real_pkg = _P(rb.__file__).resolve().parent
        tmp_wheel = work / "real2.whl"
        raw = work / "real2.raw.whl"
        with zipfile.ZipFile(raw, "w") as z:
            for rel, path in _iter_core_inputs(real_pkg):
                z.writestr("workspace_bridge/" + rel, path.read_bytes())
            z.writestr("workspace_bridge/static/dist/release.json",
                       (real_pkg / "static" / "dist" / "release.json").read_bytes())
            z.writestr("workspace_bridge-0.1.0.dist-info/METADATA",
                       "Metadata-Version: 2.1\nName: workspace-bridge\n")
        rb._normalize_wheel(src=raw, dest=tmp_wheel)
        raw.unlink()
        pi_names = rb.list_pi_inputs()
        tmp_pi = work / "real2.tar.gz"
        rb._create_pi_archive(src_root=rb.PI_ROOT, names=pi_names, dest=tmp_pi)
        return rb.assemble_bundle(output=Path(output), manifest=manifest,
                                  wheel_path=tmp_wheel,
                                  pi_archive_path=tmp_pi)
    monkeypatch.setattr(rb, "build_bundle", _fake_build_bundle)
    assert cli_main(["--state", str(missing_state),
                     "release", "build", "--output", str(out)]) is None
    assert "Release bundle: COMPLETE" in capsys.readouterr().out
    assert not missing_state.exists()
    assert (out / "bundle.json").exists()
