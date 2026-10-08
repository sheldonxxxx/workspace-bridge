#!/usr/bin/env python3
"""Build an app-bound, single-skill plugin with private local identity/version state.

Initialize once with the exact existing package name and version. Settings can
come from .env.skill-plugin, process environment, or CLI overrides. Byte-identical
skills produce no replacement ZIP. Only --init selects initialization.
No OpenAI APIs, cache discovery, tunnel changes, or uploads.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import fcntl
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from zipfile import BadZipFile, ZIP_STORED, ZipFile, ZipInfo

REPO = Path(__file__).resolve().parents[1]
DEFAULT_SKILL = REPO / "workspace_bridge/skills/project-lead/SKILL.md"
DEFAULT_TEMPLATE = REPO / "packaging/chatgpt/plugin.json"
DEFAULT_ENV_FILE = REPO / ".env.skill-plugin"
ENV_PREFIX = "WB_SKILL_PLUGIN_"
ENV_OPTIONS = {
    "app_id": ENV_PREFIX + "APP_ID",
    "package_name": ENV_PREFIX + "PACKAGE_NAME",
    "package_version": ENV_PREFIX + "PACKAGE_VERSION",
    "skill_already_packaged": ENV_PREFIX + "ALREADY_PACKAGED",
    "state": ENV_PREFIX + "STATE",
    "app_alias": ENV_PREFIX + "APP_ALIAS",
    "template": ENV_PREFIX + "TEMPLATE",
    "skill": ENV_PREFIX + "SKILL",
    "output": ENV_PREFIX + "OUTPUT",
}
PATH_OPTIONS = {"state", "template", "skill", "output"}
SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
PACKAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
ALIAS_NAME = re.compile(r"(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z")
SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
APP_ID = re.compile(r"(?:plugin_)?asdk_app_[A-Za-z0-9][A-Za-z0-9_-]*\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
METADATA = {"name", "version", "description", "author", "homepage", "repository", "license", "keywords"}
BOOTSTRAP = "initialize with --app-id ID --init --package-name EXACT_EXISTING_NAME --package-version CURRENT_VERSION (and the same --state directory)"


class PackagingError(ValueError):
    """Controlled diagnostics; never expose app IDs or raw private state."""


def _parse_boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in ("true", "1", "yes", "on"):
        return True
    if normalized in ("false", "0", "no", "off"):
        return False
    raise PackagingError("WB_SKILL_PLUGIN_ALREADY_PACKAGED must be true/false, 1/0, yes/no, or on/off")


def _env_literal(raw: str, line_number: int) -> str:
    raw = raw.strip()
    if raw.startswith(("'", '"')):
        end = raw.find(raw[0], 1)
        if end == -1 or (raw[end + 1:].strip() and not raw[end + 1:].strip().startswith("#")):
            raise PackagingError(f"env file line {line_number}: malformed quoted value")
        value = raw[1:end]
    else:
        comment = re.search(r"(?:^|\s)#", raw)
        value = (raw[:comment.start()] if comment else raw).rstrip()
        if any(character in value for character in "'\";|&<>"):
            raise PackagingError(f"env file line {line_number}: malformed literal value")
    if "$" in value or "`" in value:
        raise PackagingError(f"env file line {line_number}: shell expansion syntax is not supported")
    return value


def read_env_file(path: Path, *, required: bool = False) -> dict:
    try:
        content = path.read_bytes().decode("utf-8-sig")
    except FileNotFoundError:
        if not required:
            return {}
        raise PackagingError("explicit --env-file is missing") from None
    except UnicodeError:
        raise PackagingError("env file must be UTF-8 text") from None
    except OSError:
        raise PackagingError("env file could not be read") from None
    if "\0" in content:
        raise PackagingError("env file must not contain NULs")
    supported = {key: option for option, key in ENV_OPTIONS.items()}
    values = {}
    for number, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^export[ \t]+", "", line)
        key, separator, raw = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise PackagingError(f"env file line {number}: expected KEY=VALUE")
        if key.startswith(ENV_PREFIX) and key not in supported:
            raise PackagingError(f"env file line {number}: unknown WB_SKILL_PLUGIN_ configuration key")
        value = _env_literal(raw, number)
        if key not in supported:
            continue
        if key in values:
            raise PackagingError(f"env file line {number}: duplicate supported configuration key")
        option = supported[key]
        if option == "skill_already_packaged":
            value = _parse_boolean(value)
        elif option in PATH_OPTIONS:
            configured = Path(value)
            anchored = configured if configured.is_absolute() else path.absolute().parent / configured
            # Normalize relative components without following output symlinks;
            # the builder's original publication guards must still see them.
            value = Path(os.path.abspath(anchored))
        values[key] = value
    return values


def resolve_configuration(args: argparse.Namespace) -> argparse.Namespace:
    selected = args.env_file if args.env_file is not None else DEFAULT_ENV_FILE
    file_values = read_env_file(selected, required=args.env_file is not None)
    process_keys = {key for key in os.environ if key.startswith(ENV_PREFIX)}
    if process_keys - set(ENV_OPTIONS.values()):
        raise PackagingError("unknown WB_SKILL_PLUGIN_ configuration key in process environment")
    process_values = {}
    for option, key in ENV_OPTIONS.items():
        if key not in process_keys:
            continue
        value = os.environ[key]
        if "\0" in value:
            raise PackagingError("process configuration must not contain NULs")
        if option == "skill_already_packaged":
            value = _parse_boolean(value)
        elif option in PATH_OPTIONS:
            value = Path(value)
        process_values[key] = value
    resolved = argparse.Namespace(**vars(args))
    for option, key in ENV_OPTIONS.items():
        cli_value = getattr(args, option)
        default = False if option == "skill_already_packaged" else None
        value = cli_value if cli_value is not None else process_values.get(key, file_values.get(key, default))
        setattr(resolved, option, value)
    return resolved


def normalize_app_id(value: str) -> str:
    if not isinstance(value, str) or not APP_ID.fullmatch(value):
        raise PackagingError("app ID must be plugin_asdk_app_... or asdk_app_... with a letter/digit suffix and only letters, digits, '_' or '-'")
    return value.removeprefix("plugin_")


def _valid_name(value, pattern) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 64 and pattern.fullmatch(value) is not None


def _valid_version(value) -> bool:
    return isinstance(value, str) and VERSION.fullmatch(value) is not None


def validate_skill_identity(package_name: str, skill_name: str) -> None:
    if len(f"{package_name}:{skill_name}") > 64:
        raise PackagingError("combined plugin-name:skill-name identity must be 64 characters or fewer")


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PackagingError("JSON contains duplicate keys")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise PackagingError("JSON contains a non-JSON numeric value")


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=_unique_json,
                           parse_constant=_invalid_constant)
    except (ValueError, UnicodeError):
        raise PackagingError("JSON must be a valid object with unique keys") from None
    if not isinstance(value, dict):
        raise PackagingError("JSON must be an object")
    return value


def read_skill(path: Path) -> tuple[bytes, dict]:
    try:
        import yaml
    except ImportError:
        raise PackagingError("PyYAML is required; use an environment with the repository test extra") from None

    class UniqueLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            self.flatten_mapping(node)
            result = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if key in result:
                    raise PackagingError("skill frontmatter contains duplicate keys")
                result[key] = self.construct_object(value_node, deep=deep)
            return result

    content = path.read_bytes()
    try:
        lines = content.decode("utf-8").splitlines()
        if not lines or lines[0] != "---" or b"\0" in content:
            raise PackagingError("SKILL.md requires UTF-8 YAML frontmatter and text instructions")
        end = lines.index("---", 1)
        frontmatter = yaml.load("\n".join(lines[1:end]), Loader=UniqueLoader)
    except PackagingError:
        raise
    except (UnicodeError, ValueError, TypeError, yaml.YAMLError):
        raise PackagingError("SKILL.md has invalid or unterminated YAML frontmatter") from None
    if not isinstance(frontmatter, dict) or not _valid_name(frontmatter.get("name"), SKILL_NAME):
        raise PackagingError("skill name must be 1-64 lowercase letters/digits separated by single hyphens")
    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise PackagingError("skill description must be a nonempty string of at most 1024 characters")
    version = frontmatter.get("version")
    if "version" in frontmatter and (not isinstance(version, str) or not version.strip()):
        raise PackagingError("skill version, when supplied, must be a nonempty string")
    if not "\n".join(lines[end + 1:]).strip():
        raise PackagingError("SKILL.md must contain instructions after frontmatter")
    return content, {"name": frontmatter["name"], "version": version}


def validate_manifest(manifest: dict) -> None:
    if (not isinstance(manifest, dict) or manifest.get("$schema") != SCHEMA
            or set(manifest) - METADATA - {"$schema", "extensions"}
            or not _valid_name(manifest.get("name"), PACKAGE_NAME) or not _valid_version(manifest.get("version"))):
        raise PackagingError("template requires an Agent Plugins 1.0 manifest, a valid name, and stable MAJOR.MINOR.PATCH version")
    for key in ("description", "homepage", "repository", "license"):
        if key in manifest and not isinstance(manifest[key], str):
            raise PackagingError("manifest string metadata has an incompatible type")
    author = manifest.get("author", {})
    if (not isinstance(author, dict) or set(author) - {"name", "email", "url"}
            or any(not isinstance(value, str) for value in author.values())):
        raise PackagingError("manifest author supports only string name/email/url fields")
    keywords = manifest.get("keywords", [])
    if not isinstance(keywords, list) or any(not isinstance(value, str) for value in keywords):
        raise PackagingError("manifest keywords must be an array of strings")
    extensions = manifest.get("extensions")
    if not isinstance(extensions, dict) or any(not isinstance(v, dict) for v in extensions.values()):
        raise PackagingError("manifest extensions must contain namespace objects")
    openai = extensions.get("com.openai", {})
    if openai.get("apps") != "./.app.json" or set(openai) - {"apps", "interface"}:
        raise PackagingError("this builder supports only self-contained presentation and apps: './.app.json' in the OpenAI extension")
    interface = openai.get("interface", {})
    if not isinstance(interface, dict):
        raise PackagingError("manifest interface must be an object")
    for key in ("displayName", "longDescription", "developerName", "category", "websiteURL", "privacyPolicyURL", "termsOfServiceURL"):
        if key in interface and not isinstance(interface[key], str):
            raise PackagingError("manifest interface text fields must be strings")
    if any(key in interface for key in ("logo", "logoDark", "composerIcon", "composerIconDark", "screenshots")):
        raise PackagingError("this single-skill builder does not bundle referenced assets")
    short = interface.get("shortDescription", "")
    if not isinstance(short, str) or len(short) > 30:
        raise PackagingError("interface shortDescription must be a string of at most 30 characters")
    if "defaultPrompt" in interface:
        prompts = interface["defaultPrompt"]
        if not (isinstance(prompts, str) or (isinstance(prompts, list) and 1 <= len(prompts) <= 3
                                           and all(isinstance(v, str) for v in prompts))):
            raise PackagingError("interface defaultPrompt must be a string or one to three strings")


def default_state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        base = Path(xdg)
        if not base.is_absolute():
            raise PackagingError("XDG_STATE_HOME must be an absolute directory")
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path.home() / ".local/state"
    return base / "workspace-bridge/skill-plugins"


def state_path(directory: Path, app_id: str) -> Path:
    return directory / (sha256(normalize_app_id(app_id).encode()).hexdigest() + ".json")


@contextmanager
def _state_lock(directory: Path, app_id: str, *, initializing=False):
    if initializing:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    elif not directory.is_dir() or not state_path(directory, app_id).is_file():
        raise PackagingError(f"packager state is missing; {BOOTSTRAP}")
    lock_path = state_path(directory, app_id).with_suffix(".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PackagingError("another packager is using this app state; retry after it finishes") from None
        yield


def _save_state(path: Path, value: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".skill-state-", delete=False) as staged:
            temporary = Path(staged.name)
            staged.write(_json_bytes(value))
            staged.flush()
            os.fsync(staged.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def initialize(app_id: str, directory: Path, package_name: str, package_version: str,
               *, template: Path = DEFAULT_TEMPLATE, app_alias: str | None = None,
               skill_path: Path = DEFAULT_SKILL, skill_already_packaged: bool = False) -> Path:
    app_id = normalize_app_id(app_id)
    if not _valid_name(package_name, PACKAGE_NAME) or not _valid_version(package_version):
        raise PackagingError("bootstrap requires the exact valid package name and a stable MAJOR.MINOR.PATCH current version")
    manifest = _read_json(template)
    validate_manifest(manifest)
    alias = app_alias if app_alias is not None else manifest["name"]
    if not _valid_name(alias, ALIAS_NAME):
        raise PackagingError("app alias must be a valid 1-64 character conservative lowercase alias")
    manifest.update(name=package_name, version=package_version)
    value = {"format_version": 1, "app_id": app_id, "package_name": package_name,
             "package_version": package_version, "app_alias": alias, "manifest": manifest,
             "skill_path": str(skill_path.resolve()),
             "skill_baseline_origin": "unpackaged", "last_skill_sha256": None,
             "skill_name": None, "skill_version": None, "last_artifact": None}
    if skill_already_packaged:
        content, skill = read_skill(skill_path)
        validate_skill_identity(package_name, skill["name"])
        value.update(skill_baseline_origin="existing_release", last_skill_sha256=sha256(content).hexdigest(),
                     skill_name=skill["name"], skill_version=skill["version"])
    path = state_path(directory, app_id)
    with _state_lock(directory, app_id, initializing=True):
        if path.exists() or path.is_symlink():
            raise PackagingError("state already exists; preserve it, or bootstrap in a separate --state directory using the verified current identity/version")
        _save_state(path, value)
    return path


def _load_state(path: Path, app_id: str) -> dict:
    try:
        if path.is_symlink():
            raise PackagingError("state must not be a symlink")
        value = _read_json(path)
        if (type(value.get("format_version")) is not int or value["format_version"] != 1
                or value.get("app_id") != app_id or not _valid_name(value.get("package_name"), PACKAGE_NAME)
                or not _valid_name(value.get("app_alias"), ALIAS_NAME) or not _valid_version(value.get("package_version"))
                or not isinstance(value.get("skill_path"), str) or not Path(value["skill_path"]).is_absolute()):
            raise PackagingError("state identity/version is missing or invalid")
        manifest = value["manifest"]
        validate_manifest(manifest)
        if manifest["name"] != value["package_name"] or manifest["version"] != value["package_version"]:
            raise PackagingError("state identity/version is ambiguous")
        digest = value["last_skill_sha256"]
        artifact = value["last_artifact"]
        skill_version = value["skill_version"]
        # Older format-1 state lacks this marker: retain its original strict
        # empty-baseline/generated-artifact rules, never infer an imported baseline.
        origin = value.get("skill_baseline_origin", "unpackaged" if digest is None else "generated")
        if origin not in ("unpackaged", "existing_release", "generated"):
            raise PackagingError("skill baseline origin is invalid")
        if origin == "unpackaged":
            if digest is not None or artifact is not None or value["skill_name"] is not None or skill_version is not None:
                raise PackagingError("bootstrap state has inconsistent packaged-skill fields")
        else:
            if (not isinstance(digest, str) or not DIGEST.fullmatch(digest)
                    or not _valid_name(value["skill_name"], SKILL_NAME)
                    or (skill_version is not None and (not isinstance(skill_version, str) or not skill_version.strip()))):
                raise PackagingError("packaged-skill state is inconsistent")
            if origin == "existing_release":
                if artifact is not None:
                    raise PackagingError("existing-release baseline must not claim a generated artifact")
            elif (not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str)
                  or not Path(artifact["path"]).is_absolute() or not isinstance(artifact.get("sha256"), str)
                  or not DIGEST.fullmatch(artifact["sha256"]) or artifact.get("version") != value["package_version"]):
                raise PackagingError("generated-artifact state is inconsistent")
        return value
    except (PackagingError, KeyError, TypeError, OSError):
        raise PackagingError(f"packager state is invalid; restore a trusted backup or use a separate --state directory and {BOOTSTRAP}") from None


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _package_files(value: dict, content: bytes, skill: dict, version: str) -> dict[str, bytes]:
    manifest = deepcopy(value["manifest"])
    manifest["version"] = version
    validate_manifest(manifest)
    legacy = {key: deepcopy(v) for key, v in manifest.items() if key in METADATA}
    legacy.update(deepcopy(manifest["extensions"]["com.openai"]))
    other_extensions = {k: v for k, v in manifest["extensions"].items() if k != "com.openai"}
    if other_extensions:
        legacy["extensions"] = deepcopy(other_extensions)
    legacy["skills"] = "./skills/"
    return {"plugin.json": _json_bytes(manifest), ".codex-plugin/plugin.json": _json_bytes(legacy),
            ".app.json": _json_bytes({"apps": {value["app_alias"]: {"id": value["app_id"]}}}),
            f"skills/{skill['name']}/SKILL.md": content}


def _archive_bytes(files: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_STORED) as archive:
        for path in sorted(files):
            info = ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, files[path])
    return buffer.getvalue()


def _validate_archive(content: bytes, files: dict[str, bytes]) -> None:
    with ZipFile(BytesIO(content)) as archive:
        if (archive.namelist() != sorted(files) or archive.testzip() is not None
                or any(archive.read(path) != data for path, data in files.items())):
            raise PackagingError("generated archive failed content/layout validation")


def _publish_archive(output: Path, content: bytes, files: dict[str, bytes]) -> None:
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".skill-plugin-", suffix=".zip") as staged:
        staged.write(content)
        staged.flush()
        os.fsync(staged.fileno())
        _validate_archive(Path(staged.name).read_bytes(), files)
        try:
            os.link(staged.name, output)
        except FileExistsError:
            # Recover an archive written successfully before a failed state save.
            if output.is_symlink() or not output.is_file() or output.read_bytes() != content:
                raise PackagingError("output already exists with different content; choose another --output path") from None
    _validate_archive(output.read_bytes(), files)


def build(app_id: str, directory: Path, *, skill_path: Path | None = None,
          output: Path | None = None) -> tuple[Path, str] | None:
    app_id = normalize_app_id(app_id)
    path = state_path(directory, app_id)
    with _state_lock(directory, app_id):
        value = _load_state(path, app_id)
        content, skill = read_skill(skill_path if skill_path is not None else Path(value["skill_path"]))
        validate_skill_identity(value["package_name"], skill["name"])
        digest = sha256(content).hexdigest()
        if digest == value["last_skill_sha256"]:
            return None
        major, minor, patch = VERSION.fullmatch(value["package_version"]).groups()
        version = f"{major}.{minor}.{int(patch) + 1}"
        files = _package_files(value, content, skill, version)
        archive = _archive_bytes(files)
        if output is None:
            folder = directory / "artifacts" / path.stem
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            output = folder / f"{value['package_name']}-{version}.zip"
        if output.is_symlink():
            raise PackagingError("output must not be a symlink")
        output = output.resolve()
        _publish_archive(output, archive, files)
        value["package_version"] = value["manifest"]["version"] = version
        value.update(skill_baseline_origin="generated", last_skill_sha256=digest,
                     skill_name=skill["name"], skill_version=skill["version"],
                     last_artifact={"path": str(output), "sha256": sha256(archive).hexdigest(), "version": version})
        try:
            _save_state(path, value)
        except OSError:
            raise PackagingError("ZIP was written and validated but state could not be saved; keep the ZIP and retry the same build/output after fixing state permissions") from None
    return output, version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, help="Alternate config file; default: repository .env.skill-plugin if present; never Docker .env")
    parser.add_argument("--app-id", help="Registered technical ID; may come from WB_SKILL_PLUGIN_APP_ID")
    parser.add_argument("--state", type=Path, help="Private state directory; default: platform/XDG user-local state")
    parser.add_argument("--init", action="store_true", help="Bootstrap exact existing package identity/version once; emits no ZIP")
    parser.add_argument("--skill-already-packaged", action=argparse.BooleanOptionalAction, default=None,
                        help="With --init: declare the existing release embeds the exact selected skill bytes; seed the digest so unchanged builds no-op")
    parser.add_argument("--package-name", help="With --init: exact existing package name, never derived from app ID")
    parser.add_argument("--package-version", help="With --init: verified current stable MAJOR.MINOR.PATCH package version")
    parser.add_argument("--app-alias", help="With --init: fixed app alias; defaults to the template's name")
    parser.add_argument("--template", type=Path, help="With --init: self-contained root manifest template; snapshot stored locally")
    parser.add_argument("--skill", type=Path, help="With --init: remember canonical SKILL.md (default: project-lead); otherwise override saved source for this build")
    parser.add_argument("--output", type=Path, help="Changed-skill ZIP path; default: private state artifacts directory")
    args = parser.parse_args(argv)
    if args.init and args.output is not None:
        parser.error("--init does not accept --output")
    if not args.init and (args.skill_already_packaged is not None
                         or any(v is not None for v in (args.package_name, args.package_version, args.app_alias, args.template))):
        parser.error("identity, alias, template, and --skill-already-packaged options are only accepted with --init")
    try:
        args = resolve_configuration(args)
        if not args.app_id:
            raise PackagingError("app ID is required; use --app-id or WB_SKILL_PLUGIN_APP_ID")
        if args.init and (not args.package_name or not args.package_version):
            raise PackagingError("--init requires package name/version via CLI or WB_SKILL_PLUGIN_PACKAGE_NAME/PACKAGE_VERSION")
        directory = args.state if args.state is not None else default_state_dir()
        if args.init:
            initialize(args.app_id, directory, args.package_name, args.package_version,
                       template=args.template or DEFAULT_TEMPLATE, app_alias=args.app_alias,
                       skill_path=args.skill if args.skill is not None else DEFAULT_SKILL,
                       skill_already_packaged=args.skill_already_packaged)
            print("Initialized private package state; no ZIP created. Build again with the same configuration/state directory and without --init.")
        else:
            result = build(args.app_id, directory, skill_path=args.skill, output=args.output)
            if result is None:
                print("No change: skill bytes match the last packaged digest; no version bump or replacement ZIP.")
            else:
                path, version = result
                print(f"Created {path} (package version {version}). Reuse this ZIP if upload fails.")
    except PackagingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, BadZipFile, RuntimeError, OverflowError):
        print("error: could not read/write packager files or validate the ZIP; state was not advanced", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
