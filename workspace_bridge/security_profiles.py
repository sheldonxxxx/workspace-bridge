"""One security profile format for every agent plugin.

Profiles are Bridge-owned and assigned per workspace route. Every run carries
the rules it started with; the plugin compiles them to its own mechanism and
enforces them on every tool call. A plugin's manifest lists the keys it can
enforce, and a route whose profile restricts a key its plugin cannot enforce
is refused.

Evaluation order, most specific first: the fixed protected paths and the
profile's ``protected`` globs, then ``commands`` (shell) and
``external_roots`` (paths outside the workspace), then the category mode.
The same rules are implemented for Pi in ``runtime/pi-host-adapter``; both
implementations are checked against ``tests/fixtures/profile_vectors.json``.
"""
from __future__ import annotations

import json
import os
import re
from hashlib import sha256
from typing import Any

from .security import BridgeError

MODES = ("deny", "ask", "allow")
_RANK = {"allow": 0, "ask": 1, "deny": 2}
CATEGORIES = ("read_inside", "read_outside", "write_inside", "write_outside",
              "shell", "web", "extensions")
LIST_KEYS = ("protected", "external_roots", "commands")
KEYS = CATEGORIES + LIST_KEYS
PROFILE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")
MAX_PATTERNS = 64
MAX_PATTERN_CHARS = 400
MAX_ROOTS = 32
MAX_ROOT_CHARS = 1024
MAX_COMMANDS = 64
MAX_COMMAND_CHARS = 200
MAX_SHELL_COMMAND_CHARS = 16384
_TEMPLATE_SUFFIXES = (".example", ".sample", ".template")
_PATTERN_CHARS = re.compile(r"[A-Za-z0-9_.\-/*?\[\]!+@#%^()=:,{} ]+")
_SHELL_SPLIT = re.compile(r"&&|\|\||[;&|\n()`]|\$\(")
# ``zsh -lc '<script>'`` and the like: a shell running a script given inline.
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_SCRIPT_FLAG = re.compile(r"-[A-Za-z]*c[A-Za-z]*")
_MAX_SHELL_NESTING = 4

BUILTINS: dict[str, dict] = {
    "read-only": {
        "read_inside": "allow", "read_outside": "deny",
        "write_inside": "deny", "write_outside": "deny",
        "shell": "deny", "web": "deny", "extensions": "deny",
        "protected": [], "external_roots": [], "commands": []},
    "reviewed": {
        "read_inside": "allow", "read_outside": "ask",
        "write_inside": "allow", "write_outside": "deny",
        "shell": "ask", "web": "ask", "extensions": "ask",
        "protected": [], "external_roots": [], "commands": []},
    "trusted": {
        "read_inside": "allow", "read_outside": "allow",
        "write_inside": "allow", "write_outside": "ask",
        "shell": "allow", "web": "allow", "extensions": "allow",
        "protected": [], "external_roots": [], "commands": []},
}
DESCRIPTIONS = {
    "read-only": "Reads the workspace; no edits, shell, web or extensions.",
    "reviewed": "Edits the workspace; asks before shell, web, extensions and outside reads.",
    "trusted": "Edits and runs commands freely; asks before writing outside the workspace.",
}


def _bad(message: str) -> BridgeError:
    return BridgeError(message, "invalid_profile")


def _mode(value: Any, where: str) -> str:
    if not isinstance(value, str) or value not in MODES:
        raise _bad(f"{where} must be deny, ask or allow")
    return value


def _pattern(value: Any) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise _bad("protected entries must be text")
    text = value.strip().replace("\\", "/")
    if not text or len(text) > MAX_PATTERN_CHARS:
        raise _bad(f"protected entries must be 1 to {MAX_PATTERN_CHARS} characters")
    if text.startswith("/") or text.startswith("~"):
        raise _bad("protected entries are workspace-relative globs")
    if ".." in text.split("/"):
        raise _bad("protected entries must not contain '..'")
    if not _PATTERN_CHARS.fullmatch(text):
        raise _bad("protected entry uses unsupported characters")
    return text


def _root(value: Any, index: int) -> dict:
    if not isinstance(value, dict) or not {"path"} <= set(value) <= {"path", "read", "write"}:
        raise _bad(f"external root {index + 1} needs path and optional read and write")
    raw = value.get("path")
    if not isinstance(raw, str) or "\x00" in raw:
        raise _bad(f"external root {index + 1} path must be text")
    path = raw.strip()
    if not path or len(path) > MAX_ROOT_CHARS:
        raise _bad(f"external root {index + 1} path must be 1 to {MAX_ROOT_CHARS} characters")
    if not (path.startswith("/") or path == "~" or path.startswith("~/")):
        raise _bad(f"external root {index + 1} path must be absolute or start with ~/")
    if ".." in path.split("/") or "//" in path or (path.endswith("/") and path != "/"):
        raise _bad(f"external root {index + 1} path is not normalized")
    root = {"path": path}
    for key in ("read", "write"):
        if key in value:
            root[key] = _mode(value[key], f"external root {index + 1} {key}")
    return root


def _command(value: Any, index: int) -> dict:
    if not isinstance(value, dict) or set(value) != {"prefix", "mode"}:
        raise _bad(f"command rule {index + 1} needs prefix and mode")
    prefix = value.get("prefix")
    if not isinstance(prefix, str) or "\x00" in prefix or "\n" in prefix:
        raise _bad(f"command rule {index + 1} prefix must be one line of text")
    prefix = " ".join(prefix.split())
    if not prefix or len(prefix) > MAX_COMMAND_CHARS:
        raise _bad(f"command rule {index + 1} prefix must be 1 to {MAX_COMMAND_CHARS} characters")
    return {"prefix": prefix, "mode": _mode(value.get("mode"), f"command rule {index + 1} mode")}


def validate(raw: Any) -> dict:
    """Return the canonical rules for one profile, or raise ``invalid_profile``."""
    if not isinstance(raw, dict):
        raise _bad("Profile rules must be an object")
    unknown = set(raw) - set(KEYS)
    if unknown:
        raise _bad("Unknown profile keys: " + ", ".join(sorted(unknown)))
    missing = [key for key in CATEGORIES if key not in raw]
    if missing:
        raise _bad("Profile needs " + ", ".join(missing))
    rules: dict = {key: _mode(raw[key], key) for key in CATEGORIES}
    protected = raw.get("protected", [])
    if not isinstance(protected, list) or len(protected) > MAX_PATTERNS:
        raise _bad(f"protected must be a list of at most {MAX_PATTERNS} globs")
    rules["protected"] = sorted({_pattern(item) for item in protected})
    roots = raw.get("external_roots", [])
    if not isinstance(roots, list) or len(roots) > MAX_ROOTS:
        raise _bad(f"external_roots must be a list of at most {MAX_ROOTS} roots")
    rules["external_roots"] = [_root(item, index) for index, item in enumerate(roots)]
    if len({root["path"] for root in rules["external_roots"]}) != len(rules["external_roots"]):
        raise _bad("external_roots repeats a path")
    commands = raw.get("commands", [])
    if not isinstance(commands, list) or len(commands) > MAX_COMMANDS:
        raise _bad(f"commands must be a list of at most {MAX_COMMANDS} rules")
    rules["commands"] = [_command(item, index) for index, item in enumerate(commands)]
    if len({rule["prefix"] for rule in rules["commands"]}) != len(rules["commands"]):
        raise _bad("commands repeats a prefix")
    if len(json.dumps(rules)) > 32768:
        raise _bad("Profile is too large")
    return rules


def canonical_json(rules: dict) -> str:
    return json.dumps(rules, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def revision(rules: dict) -> str:
    return "prof_" + sha256(canonical_json(rules).encode()).hexdigest()[:32]


def restricted_keys(rules: dict) -> list[str]:
    """Keys whose value needs the plugin's enforcement (not plain ``allow``)."""
    keys = [key for key in CATEGORIES if rules[key] != "allow"]
    if rules["protected"]:
        keys.append("protected")
    # A plugin that ignores a root or a command rule falls back to the
    # category mode, which is only unsafe when the rule is stricter.
    if any(_RANK[root[kind]] > _RANK[rules[f"{kind}_outside"]]
           for root in rules["external_roots"] for kind in ("read", "write") if kind in root):
        keys.append("external_roots")
    if any(_RANK[rule["mode"]] > _RANK[rules["shell"]] for rule in rules["commands"]):
        keys.append("commands")
    return keys


def unenforced(rules: dict, enforces: list[str] | tuple[str, ...]) -> list[str]:
    """Restricted keys this plugin cannot enforce; non-empty means refuse."""
    return [key for key in restricted_keys(rules) if key not in set(enforces)]


def loosened(old: dict, new: dict) -> list[str]:
    """Keys the new rules make more permissive than the old ones."""
    keys = [key for key in CATEGORIES if _RANK[new[key]] < _RANK[old[key]]]
    if set(old["protected"]) - set(new["protected"]):
        keys.append("protected")
    old_roots = {root["path"]: root for root in old["external_roots"]}
    new_roots = {root["path"]: root for root in new["external_roots"]}
    if any(_RANK[new_roots.get(path, {}).get(kind, new[f"{kind}_outside"])]
           < _RANK[old_roots.get(path, {}).get(kind, old[f"{kind}_outside"])]
           for path in set(old_roots) | set(new_roots) for kind in ("read", "write")):
        keys.append("external_roots")
    old_commands = {rule["prefix"]: rule["mode"] for rule in old["commands"]}
    new_commands = {rule["prefix"]: rule["mode"] for rule in new["commands"]}
    if any(_RANK[mode] < _RANK[old_commands.get(prefix, old["shell"])]
           for prefix, mode in new_commands.items()) or any(
            _RANK[new["shell"]] < _RANK[mode] and prefix not in new_commands
            for prefix, mode in old_commands.items()):
        keys.append("commands")
    return keys


def stricter(*modes: str) -> str:
    return max(modes, key=lambda mode: _RANK[mode])


# ---------------------------------------------------------------------------
# Evaluation helpers shared by the Python plugins (Claude Code, Codex,
# OpenCode). Pi carries the same logic in JavaScript.

def _glob_regex(pattern: str) -> re.Pattern:
    out, index = "", 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index:index + 3] == "**/":
                out += "(?:.*/)?"
                index += 3
            elif pattern[index:index + 2] == "**":
                out += ".*"
                index += 2
            else:
                out += "[^/]*"
                index += 1
        elif char == "?":
            out += "[^/]"
            index += 1
        elif char == "[":
            close = pattern.find("]", index + 1)
            if close == -1:
                out += re.escape(char)
                index += 1
            else:
                body = pattern[index + 1:close]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out += "[" + body.replace("\\", "\\\\") + "]"
                index = close + 1
        else:
            out += re.escape(char)
            index += 1
    return re.compile(out)


def glob_matches(pattern: str, relative: str) -> bool:
    """Whether a workspace-relative path, or a folder containing it, matches.

    A pattern without ``/`` matches a name at any depth, as in ``.gitignore``;
    a pattern with ``/`` is anchored at the workspace root.
    """
    parts = [part for part in relative.split("/") if part and part != "."]
    if not parts:
        return False
    try:
        regex = _glob_regex(pattern.rstrip("/"))
    except re.error:
        return True
    if "/" not in pattern.rstrip("/"):
        return any(regex.fullmatch(part) for part in parts)
    return any(regex.fullmatch("/".join(parts[:end])) for end in range(1, len(parts) + 1))


def fixed_protected(relative: str) -> str | None:
    """Paths no profile can open: Git internals, env files and checks.toml."""
    parts = [part for part in relative.replace("\\", "/").split("/") if part and part != "."]
    if ".git" in parts:
        return "Git internals are protected"
    if any((part == ".env" or part.startswith(".env.")) and not part.endswith(_TEMPLATE_SUFFIXES)
           for part in parts):
        return "Environment files are protected"
    if parts == ["checks.toml"]:
        return "checks.toml is protected"
    return None


def _inside(root: str, candidate: str) -> bool:
    return candidate == root or candidate.startswith(root.rstrip(os.sep) + os.sep)


def resolve_path(root: str, raw: Any) -> str | None:
    """Absolute real path of one tool path; missing leaves resolve via the parent.

    Only ``~`` and ``~/`` expand; another user's ``~name`` is refused.
    """
    if not isinstance(raw, str) or not raw or len(raw) > 4096 or "\x00" in raw:
        return None
    if raw.startswith("~") and raw != "~" and not raw.startswith("~/"):
        return None
    expanded = os.path.expanduser(raw) if raw.startswith("~") else raw
    candidate = expanded if os.path.isabs(expanded) else os.path.join(root, expanded)
    try:
        return os.path.realpath(candidate)
    except (OSError, ValueError):
        return None


def _real_root(path: str) -> str:
    try:
        return os.path.realpath(os.path.expanduser(path))
    except (OSError, ValueError):
        return ""


def path_mode(rules: dict, root: str, raw: Any, *, write: bool) -> tuple[str, str]:
    """``(mode, reason)`` for reading or writing one path; never raises."""
    try:
        root_real = os.path.realpath(root)
    except (OSError, ValueError):
        return "deny", "Workspace directory is unavailable"
    resolved = resolve_path(root_real, raw)
    if resolved is None:
        return "deny", "Tool path is invalid"
    kind = "write" if write else "read"
    if _inside(root_real, resolved):
        relative = os.path.relpath(resolved, root_real).replace(os.sep, "/")
        relative = "" if relative == "." else relative
        reason = fixed_protected(relative)
        if reason:
            return "deny", reason
        if any(glob_matches(pattern, relative) for pattern in rules["protected"]):
            return "deny", "Path is protected by this security profile"
        mode = rules[f"{kind}_inside"]
        return mode, "" if mode != "deny" else f"Workspace {kind}s are disabled by this security profile"
    if fixed_protected(resolved):
        return "deny", "Path is protected"
    best = None
    for entry in rules["external_roots"]:
        real = _real_root(entry["path"])
        if real and _inside(real, resolved) and (best is None or len(real) > len(best[0])):
            best = (real, entry)
    mode = (best[1] if best else {}).get(kind, rules[f"{kind}_outside"])
    return mode, "" if mode != "deny" else f"Outside {kind}s are disabled by this security profile"


def _matches_prefix(segment: str, prefix: str) -> bool:
    return segment == prefix or segment.startswith(prefix + " ")


def _shell_words(text: str) -> list[str] | None:
    """POSIX-style words of ``text``, or ``None`` when its quoting is unbalanced."""
    words: list[str] = []
    word: list[str] = []
    quote = ""
    started = False
    index = 0
    while index < len(text):
        char = text[index]
        if quote == "'":
            if char == "'":
                quote = ""
            else:
                word.append(char)
        elif quote == '"':
            if char == '"':
                quote = ""
            elif char == "\\" and index + 1 < len(text) and text[index + 1] in '"\\$`':
                index += 1
                word.append(text[index])
            else:
                word.append(char)
        elif char in "'\"":
            quote, started = char, True
        elif char == "\\":
            if index + 1 == len(text):
                return None
            index += 1
            word.append(text[index])
        elif char in " \t\n":
            if word or started:
                words.append("".join(word))
            word, started = [], False
        else:
            word.append(char)
        index += 1
    if quote:
        return None
    if word or started:
        words.append("".join(word))
    return words


def _runs_script(words: list[str]) -> bool:
    return (len(words) >= 2 and os.path.basename(words[0]) in _SHELLS
            and _SCRIPT_FLAG.fullmatch(words[1]) is not None)


def command_mode(rules: dict, command: Any, _depth: int = 0) -> tuple[str, str]:
    """``(mode, reason)`` for one shell command; never raises.

    A deny or ask rule applies when its prefix starts any simple command in
    the line, so ``cd x && git push`` still meets a ``git push`` rule. An
    allow rule applies only when every simple command in the line has an
    allow rule; otherwise the shell mode decides. A line that is only a shell
    running an inline script, such as ``zsh -lc 'git push'``, is judged by
    its script; a script nested deeper hides its commands, so it asks at
    least whenever the profile has ask or deny command rules.
    """
    if (not isinstance(command, str) or not command.strip()
            or len(command) > MAX_SHELL_COMMAND_CHARS or "\x00" in command):
        return "deny", "Shell command is missing or too long"
    whole = _shell_words(command)
    if (whole is not None and len(whole) == 3 and _runs_script(whole)
            and _depth < _MAX_SHELL_NESTING):
        return command_mode(rules, whole[2], _depth + 1)
    guarded = any(rule["mode"] != "allow" for rule in rules["commands"])
    segments = [" ".join(part.split()) for part in _SHELL_SPLIT.split(command)]
    segments = [segment for segment in segments if segment]
    restrictive = "allow"
    covered = bool(segments)
    for segment in segments:
        words = segment.split(" ")
        # Leading environment assignments do not change which program runs.
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", words[0]):
            words = words[1:]
        if guarded and _runs_script(words):
            restrictive = stricter(restrictive, "ask")
            covered = False
            continue
        segment = " ".join(words)
        matches = [rule for rule in rules["commands"] if _matches_prefix(segment, rule["prefix"])]
        best = max(matches, key=lambda rule: len(rule["prefix"]), default=None)
        if best is None:
            covered = False
            continue
        if best["mode"] == "allow":
            continue
        restrictive = stricter(restrictive, best["mode"])
    if restrictive == "deny":
        return "deny", "Command is denied by this security profile"
    if covered:
        return restrictive, ""
    # Simple commands without a rule fall to the shell mode.
    if rules["shell"] == "deny":
        return "deny", "Shell is disabled by this security profile"
    return stricter(restrictive, rules["shell"]), ""


def wire(profile_id: str, rules: dict) -> dict:
    """The security block a run carries to its plugin."""
    return {"source": "profile", "profile": {
        "id": profile_id, "revision": revision(rules), "rules": rules}}


def grant_read(binding: dict, folder: str, enforces: list[str] | tuple[str, ...]) -> dict:
    """A run's binding with read access to one folder outside the workspace.

    Runs use it for their handoff's read-only plan files. Writing there is
    denied when the plugin enforces ``external_roots``; otherwise the root
    only loosens reading, which a plugin may safely ignore. Native security
    and profiles already at their root limit are returned unchanged.
    """
    if binding.get("source") != "profile":
        return binding
    profile = binding["profile"]
    rules = json.loads(canonical_json(profile["rules"]))
    roots = rules["external_roots"]
    if len(roots) >= MAX_ROOTS or any(root["path"] == folder for root in roots):
        return binding
    root = {"path": folder, "read": "allow"}
    if "external_roots" in enforces:
        root["write"] = "deny"
    rules["external_roots"] = [*roots, root]
    try:
        return wire(profile["id"], validate(rules))
    except BridgeError:  # a folder path no profile could name
        return binding


def from_wire(value: Any) -> tuple[str, str, dict]:
    """Validate a run's security block on the plugin side."""
    if not isinstance(value, dict) or value.get("source") != "profile":
        raise _bad("Run security must name a profile")
    profile = value.get("profile")
    if not isinstance(profile, dict) or not isinstance(profile.get("id"), str) \
            or not PROFILE_ID.fullmatch(profile["id"]):
        raise _bad("Run security profile is invalid")
    rules = validate(profile.get("rules"))
    if profile.get("revision") != revision(rules):
        raise _bad("Run security profile revision does not match its rules")
    return profile["id"], profile["revision"], rules
