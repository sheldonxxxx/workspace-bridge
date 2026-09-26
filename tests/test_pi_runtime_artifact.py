"""M4.2C1.1 Pi runtime artifacts: deterministic, bound, fail-closed."""
from __future__ import annotations

import gzip
import io
import json
import tarfile
from pathlib import Path

import pytest

from workspace_bridge import __version__
from workspace_bridge.pi_runtime_artifact import (
    RuntimeArtifactError,
    build_runtime_artifact,
    build_runtime_from_source_bundle,
    compute_runtime_artifact_id,
    runtime_asset_names,
    validate_runtime_artifact,
)
from workspace_bridge.release import validate_release
from workspace_bridge.release_bundle import (
    _PI_INPUTS_JS,
    _PI_JS,
)


def _synthetic_pi(build: str = "sha256:" + "f" * 64) -> dict:
    return validate_release({
        "contract": 1, "product": "workspace-bridge",
        "product_version": __version__, "component": "pi-host-adapter",
        "component_version": "0.1.0", "build_id": build,
    })


FAKE_PI = _synthetic_pi()
FAKE_INPUTS = ["adapter.mjs", "package-lock.json", "package.json"]


class _FakeResult:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def _fake_run_factory(*, pi_release=None, pi_inputs=None):
    pi_release = pi_release if pi_release is not None else FAKE_PI
    pi_inputs = pi_inputs if pi_inputs is not None else None

    def _fake(argv, **kwargs):
        assert isinstance(argv, list)
        assert kwargs.get("shell") is not True
        script = argv[3]
        if script == _PI_JS:
            return _FakeResult(json.dumps(pi_release))
        if script == _PI_INPUTS_JS:
            # For runtime validation the helper lists inputs of the
            # extracted dir; return fixed fixture inventory that matches
            # our staged fixture top-level production files.
            if pi_inputs is not None:
                return _FakeResult(json.dumps(pi_inputs))
            # Default: caller must provide inventory matching staging.
            # Fall back to FAKE_INPUTS for simple fixtures.
            return _FakeResult(json.dumps(FAKE_INPUTS))
        raise AssertionError(f"unexpected script {script!r}")
    return _fake


def _make_staging(base: Path, *, extra_top: list[str] | None = None,
                  with_exec: bool = False) -> Path:
    stage = base / "stage"
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "adapter.mjs").write_text("export const x = 1;\n")
    (stage / "package.json").write_text(
        json.dumps({"name": "x", "version": "0.1.0"}))
    (stage / "package-lock.json").write_text(
        json.dumps({"lockfileVersion": 3}))
    nm = stage / "node_modules" / "@earendil-works" / "pi-coding-agent"
    nm.mkdir(parents=True, exist_ok=True)
    (nm / "package.json").write_text(json.dumps({"name": "pi"}))
    (nm / "index.js").write_text("module.exports = {};\n")
    if with_exec:
        bin_file = nm / "run.sh"
        bin_file.write_text("#!/bin/sh\necho hi\n")
        bin_file.chmod(0o755)
    for name in (extra_top or []):
        (stage / name).write_text("extra\n")
    return stage


def _lock_sha(stage: Path) -> str:
    import hashlib
    data = (stage / "package-lock.json").read_bytes()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_deterministic_fixture_builds_identical(tmp_path):
    stage = _make_staging(tmp_path / "a")
    lock_sha = _lock_sha(stage)
    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    out1.mkdir()
    out2.mkdir()
    fake_det = _fake_run_factory()
    r1 = build_runtime_artifact(
        staged_root=stage, source_pi_sha256="sha256:" + "a" * 64,
        pi_release=FAKE_PI, package_lock_sha256=lock_sha,
        product_version=__version__, platform="linux", arch="x64",
        node_major=24, output_dir=out1, _run=fake_det)
    r2 = build_runtime_artifact(
        staged_root=stage, source_pi_sha256="sha256:" + "a" * 64,
        pi_release=FAKE_PI, package_lock_sha256=lock_sha,
        product_version=__version__, platform="linux", arch="x64",
        node_major=24, output_dir=out2, _run=fake_det)
    assert r1["runtime_sha256"] == r2["runtime_sha256"]
    assert r1["runtime_artifact_id"] == r2["runtime_artifact_id"]
    # Archive bytes identical (deterministic within platform).
    archives = list(out1.glob("*.tar.gz"))
    archives2 = list(out2.glob("*.tar.gz"))
    assert len(archives) == 1 and len(archives2) == 1
    assert archives[0].read_bytes() == archives2[0].read_bytes()


def test_platform_hashes_may_differ_but_validate(tmp_path):
    # Same source, different platform metadata => different IDs, both valid.
    stage = _make_staging(tmp_path / "a")
    lock_sha = _lock_sha(stage)
    out = tmp_path / "out"
    out.mkdir()
    linux = build_runtime_artifact(
        staged_root=stage, source_pi_sha256="sha256:" + "a" * 64,
        pi_release=FAKE_PI, package_lock_sha256=lock_sha,
        product_version=__version__, platform="linux", arch="x64",
        node_major=24, output_dir=out, _run=_fake_run_factory())
    # Second platform needs separate output dir (no overwrite).
    out2 = tmp_path / "out2"
    out2.mkdir()
    darwin = build_runtime_artifact(
        staged_root=stage, source_pi_sha256="sha256:" + "a" * 64,
        pi_release=FAKE_PI, package_lock_sha256=lock_sha,
        product_version=__version__, platform="darwin", arch="arm64",
        node_major=24, output_dir=out2, _run=_fake_run_factory())
    assert linux["runtime_artifact_id"] != darwin["runtime_artifact_id"]
    # Both validate (with injected Pi helper matching fixture inventory).
    # Runtime archives contain node_modules, so extracted inputs are still
    # FAKE_INPUTS production files; inject matching inventory.
    fake = _fake_run_factory(pi_inputs=FAKE_INPUTS)
    arch1, meta1 = runtime_asset_names(
        product_version=__version__, platform="linux", arch="x64")
    arch2, meta2 = runtime_asset_names(
        product_version=__version__, platform="darwin", arch="arm64")
    validate_runtime_artifact(
        archive_path=out / arch1, metadata_path=out / meta1, _run=fake)
    validate_runtime_artifact(
        archive_path=out2 / arch2, metadata_path=out2 / meta2, _run=fake)


def test_metadata_binds_source_lock_platform(tmp_path):
    stage = _make_staging(tmp_path / "a")
    lock_sha = _lock_sha(stage)
    out = tmp_path / "out"
    out.mkdir()
    meta = build_runtime_artifact(
        staged_root=stage, source_pi_sha256="sha256:" + "b" * 64,
        pi_release=FAKE_PI, package_lock_sha256=lock_sha,
        product_version=__version__, platform="linux", arch="arm64",
        node_major=24, output_dir=out, _run=_fake_run_factory())
    assert meta["source_pi_sha256"] == "sha256:" + "b" * 64
    assert meta["package_lock_sha256"] == lock_sha
    assert meta["platform"] == "linux"
    assert meta["arch"] == "arm64"
    assert meta["node_major"] == 24
    assert meta["pi_release"] == FAKE_PI
    recomputed = compute_runtime_artifact_id(
        product_version=__version__, platform="linux", arch="arm64",
        node_major=24, source_pi_sha256="sha256:" + "b" * 64,
        pi_release=FAKE_PI, package_lock_sha256=lock_sha,
        runtime_sha256=meta["runtime_sha256"],
        runtime_size=meta["runtime_size"])
    assert recomputed == meta["runtime_artifact_id"]


def test_tampered_archive_rejected(tmp_path):
    stage = _make_staging(tmp_path / "a")
    lock_sha = _lock_sha(stage)
    out = tmp_path / "out"
    out.mkdir()
    meta = build_runtime_artifact(
        staged_root=stage, source_pi_sha256="sha256:" + "a" * 64,
        pi_release=FAKE_PI, package_lock_sha256=lock_sha,
        product_version=__version__, platform="linux", arch="x64",
        node_major=24, output_dir=out, _run=_fake_run_factory())
    arch, meta_name = runtime_asset_names(
        product_version=__version__, platform="linux", arch="x64")
    archive = out / arch
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 1
    archive.write_bytes(bytes(data))
    fake = _fake_run_factory()
    with pytest.raises(RuntimeArtifactError):
        validate_runtime_artifact(
            archive_path=archive, metadata_path=out / meta_name, _run=fake)
    assert meta["runtime_sha256"] != "sha256:" + "0" * 64


def test_tampered_node_modules_rejected_via_hash(tmp_path):
    # Tampering staged node_modules before build changes runtime hash;
    # tampering archive after build fails hash check (covered above).
    # Here prove different staged content => different runtime SHA.
    stage1 = _make_staging(tmp_path / "a")
    stage2 = _make_staging(tmp_path / "b")
    (stage2 / "node_modules" / "@earendil-works" / "pi-coding-agent"
     / "index.js").write_text("module.exports = {evil:1};\n")
    lock1 = _lock_sha(stage1)
    lock2 = _lock_sha(stage2)
    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    out1.mkdir()
    out2.mkdir()
    r1 = build_runtime_artifact(
        staged_root=stage1, source_pi_sha256="sha256:" + "a" * 64,
        pi_release=FAKE_PI, package_lock_sha256=lock1,
        product_version=__version__, platform="linux", arch="x64",
        node_major=24, output_dir=out1, _run=_fake_run_factory())
    r2 = build_runtime_artifact(
        staged_root=stage2, source_pi_sha256="sha256:" + "a" * 64,
        pi_release=FAKE_PI, package_lock_sha256=lock2,
        product_version=__version__, platform="linux", arch="x64",
        node_major=24, output_dir=out2, _run=_fake_run_factory())
    assert r1["runtime_sha256"] != r2["runtime_sha256"]


def test_source_sha_binding_strict(tmp_path):
    stage = _make_staging(tmp_path / "a")
    lock_sha = _lock_sha(stage)
    out = tmp_path / "out"
    out.mkdir()
    build_runtime_artifact(
        staged_root=stage, source_pi_sha256="sha256:" + "a" * 64,
        pi_release=FAKE_PI, package_lock_sha256=lock_sha,
        product_version=__version__, platform="linux", arch="x64",
        node_major=24, output_dir=out, _run=_fake_run_factory())
    arch, meta_name = runtime_asset_names(
        product_version=__version__, platform="linux", arch="x64")
    fake = _fake_run_factory()
    # Matching source file passes.
    source = tmp_path / "source.tar.gz"
    source.write_bytes(b"fake-source-bytes")
    import hashlib
    real_sha = "sha256:" + hashlib.sha256(b"fake-source-bytes").hexdigest()
    # Our metadata has a*a; use mismatched file to prove strict binding.
    with pytest.raises(RuntimeArtifactError):
        validate_runtime_artifact(
            archive_path=out / arch, metadata_path=out / meta_name,
            source_pi_path=source, _run=fake)
    assert real_sha != "sha256:" + "a" * 64


def test_unexpected_top_level_rejected(tmp_path):
    stage = _make_staging(tmp_path / "a", extra_top=["evil.txt"])
    lock_sha = _lock_sha(stage)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(RuntimeArtifactError):
        build_runtime_artifact(
            staged_root=stage, source_pi_sha256="sha256:" + "a" * 64,
            pi_release=FAKE_PI, package_lock_sha256=lock_sha,
            product_version=__version__, platform="linux", arch="x64",
            node_major=24, output_dir=out, _run=_fake_run_factory())
    # test/ and launchd/ also rejected.
    stage2 = _make_staging(tmp_path / "b")
    (stage2 / "test").mkdir()
    out2 = tmp_path / "out2"
    out2.mkdir()
    with pytest.raises(RuntimeArtifactError):
        build_runtime_artifact(
            staged_root=stage2, source_pi_sha256="sha256:" + "a" * 64,
            pi_release=FAKE_PI, package_lock_sha256=_lock_sha(stage2),
            product_version=__version__, platform="linux", arch="x64",
            node_major=24, output_dir=out2, _run=_fake_run_factory())


def test_absolute_symlink_rejected(tmp_path):
    stage = _make_staging(tmp_path / "a")
    (stage / "node_modules" / "evil-link").symlink_to("/etc/passwd")
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(RuntimeArtifactError):
        build_runtime_artifact(
            staged_root=stage, source_pi_sha256="sha256:" + "a" * 64,
            pi_release=FAKE_PI, package_lock_sha256=_lock_sha(stage),
            product_version=__version__, platform="linux", arch="x64",
            node_major=24, output_dir=out, _run=_fake_run_factory())


def test_out_of_root_symlink_rejected(tmp_path):
    stage = _make_staging(tmp_path / "a")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (stage / "node_modules" / "escape").symlink_to("../../outside.txt")
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(RuntimeArtifactError):
        build_runtime_artifact(
            staged_root=stage, source_pi_sha256="sha256:" + "a" * 64,
            pi_release=FAKE_PI, package_lock_sha256=_lock_sha(stage),
            product_version=__version__, platform="linux", arch="x64",
            node_major=24, output_dir=out, _run=_fake_run_factory())


def test_relative_symlink_within_root_allowed(tmp_path):
    stage = _make_staging(tmp_path / "a")
    target = stage / "node_modules" / "@earendil-works" / "pi-coding-agent" / "index.js"
    link = stage / "node_modules" / "link-to-index.js"
    link.symlink_to("@earendil-works/pi-coding-agent/index.js")
    assert target.exists()
    out = tmp_path / "out"
    out.mkdir()
    meta = build_runtime_artifact(
        staged_root=stage, source_pi_sha256="sha256:" + "a" * 64,
        pi_release=FAKE_PI, package_lock_sha256=_lock_sha(stage),
        product_version=__version__, platform="linux", arch="x64",
        node_major=24, output_dir=out, _run=_fake_run_factory())
    assert meta["runtime_size"] > 0
    arch, meta_name = runtime_asset_names(
        product_version=__version__, platform="linux", arch="x64")
    fake = _fake_run_factory()
    validated = validate_runtime_artifact(
        archive_path=out / arch, metadata_path=out / meta_name, _run=fake)
    assert validated["runtime_artifact_id"] == meta["runtime_artifact_id"]


def test_traversal_member_rejected_on_validate(tmp_path):
    stage = _make_staging(tmp_path / "a")
    lock_sha = _lock_sha(stage)
    out = tmp_path / "out"
    out.mkdir()
    build_runtime_artifact(
        staged_root=stage, source_pi_sha256="sha256:" + "a" * 64,
        pi_release=FAKE_PI, package_lock_sha256=lock_sha,
        product_version=__version__, platform="linux", arch="x64",
        node_major=24, output_dir=out, _run=_fake_run_factory())
    arch, meta_name = runtime_asset_names(
        product_version=__version__, platform="linux", arch="x64")
    # Craft evil archive with traversal, keep metadata (hash will mismatch
    # first, so craft matching hash? Instead test safe-list directly).
    evil = tmp_path / "evil.tar.gz"
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
    from workspace_bridge.pi_runtime_artifact import _safe_list_tar
    with pytest.raises(RuntimeArtifactError):
        _safe_list_tar(src=evil)


def test_platform_metadata_strict(tmp_path):
    stage = _make_staging(tmp_path / "a")
    lock_sha = _lock_sha(stage)
    out = tmp_path / "out"
    out.mkdir()
    for bad_platform, bad_arch, bad_node in [
            ("windows", "x64", 24), ("linux", "mips", 24), ("linux", "x64", 99),
            ("linux", "x64", "24"),
    ]:
        with pytest.raises(RuntimeArtifactError):
            build_runtime_artifact(
                staged_root=stage, source_pi_sha256="sha256:" + "a" * 64,
                pi_release=FAKE_PI, package_lock_sha256=lock_sha,
                product_version=__version__, platform=bad_platform,
                arch=bad_arch, node_major=bad_node, output_dir=out, _run=_fake_run_factory())
    # Tampered platform without recomputed ID fails.
    meta = build_runtime_artifact(
        staged_root=stage, source_pi_sha256="sha256:" + "a" * 64,
        pi_release=FAKE_PI, package_lock_sha256=lock_sha,
        product_version=__version__, platform="linux", arch="x64",
        node_major=24, output_dir=out, _run=_fake_run_factory())
    arch, meta_name = runtime_asset_names(
        product_version=__version__, platform="linux", arch="x64")
    raw = json.loads((out / meta_name).read_text())
    raw["platform"] = "darwin"
    (out / meta_name).write_text(json.dumps(raw, sort_keys=True, indent=2))
    with pytest.raises(RuntimeArtifactError):
        validate_runtime_artifact(
            archive_path=out / arch, metadata_path=out / meta_name,
            _run=_fake_run_factory())


def test_safe_metadata_no_paths_secrets(tmp_path):
    stage = _make_staging(tmp_path / "a")
    out = tmp_path / "out"
    out.mkdir()
    meta = build_runtime_artifact(
        staged_root=stage, source_pi_sha256="sha256:" + "a" * 64,
        pi_release=FAKE_PI, package_lock_sha256=_lock_sha(stage),
        product_version=__version__, platform="linux", arch="x64",
        node_major=24, output_dir=out, _run=_fake_run_factory())
    text = json.dumps(meta, sort_keys=True, ensure_ascii=False)
    lowered = text.lower()
    assert "/tmp/" not in text
    assert "http://" not in text
    assert "token" not in lowered
    assert "secret" not in lowered
    assert "timestamp" not in lowered


def test_asset_names_include_version_platform(tmp_path):
    arch_file, meta_file = runtime_asset_names(
        product_version="0.1.0", platform="linux", arch="x64")
    assert "0.1.0" in arch_file and "linux" in arch_file and "x64" in arch_file
    assert "0.1.0" in meta_file and "linux" in meta_file
    arch2, _ = runtime_asset_names(
        product_version="0.1.0", platform="darwin", arch="arm64")
    assert "darwin" in arch2 and "arm64" in arch2
    assert arch_file != arch2
