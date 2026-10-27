#!/usr/bin/env python3
"""Regenerate tests/fixtures/profile_vectors.json from the Python rules.

Usage: uv run python scripts/gen_profile_vectors.py tests/fixtures/profile_vectors.json

Review the diff by hand: the vectors are the contract the Pi evaluator is
checked against, so a changed expectation is a behavior change on both sides.
"""
import json, os, sys, tempfile
from workspace_bridge import security_profiles as sp
from workspace_bridge.security import BridgeError

B = sp.BUILTINS
custom = {**B["reviewed"], "protected": ["secrets/**", "*.pem", " build/ "],
          "external_roots": [{"path": "/@out/shared", "read": "allow", "write": "ask"}],
          "commands": [{"prefix": "git  push", "mode": "deny"}, {"prefix": "git status", "mode": "allow"},
                       {"prefix": "npm test", "mode": "allow"}, {"prefix": "rm", "mode": "ask"}]}
locked = {**B["read-only"], "commands": [{"prefix": "ls", "mode": "allow"}]}

canonical = [{"raw": r} for r in (B["read-only"], B["reviewed"], B["trusted"],
    {**B["trusted"], "protected": ["b", "a", "a"]},
    {**B["reviewed"], "commands": [{"prefix": " make\t build ", "mode": "ask"}]},
    {**B["reviewed"], "external_roots": [{"path": "~/notes", "write": "deny"}]},
    {k: v for k, v in B["trusted"].items() if k not in sp.LIST_KEYS})]
for case in canonical:
    case["rules"] = sp.validate(case["raw"])
    case["revision"] = sp.revision(case["rules"])

invalid = [None, [], {}, {**B["trusted"], "extra": 1}, {**B["trusted"], "shell": "maybe"},
    {k: v for k, v in B["trusted"].items() if k != "web"},
    {**B["trusted"], "protected": ["/abs"]}, {**B["trusted"], "protected": ["~/x"]},
    {**B["trusted"], "protected": ["a/../b"]}, {**B["trusted"], "protected": ["a|b"]},
    {**B["trusted"], "protected": [""]}, {**B["trusted"], "protected": "x"},
    {**B["trusted"], "external_roots": [{"path": "rel"}]},
    {**B["trusted"], "external_roots": [{"path": "/a/"}]},
    {**B["trusted"], "external_roots": [{"path": "/a//b"}]},
    {**B["trusted"], "external_roots": [{"path": "/a/../b"}]},
    {**B["trusted"], "external_roots": [{"path": "/a", "mode": "allow"}]},
    {**B["trusted"], "external_roots": [{"path": "/a"}, {"path": "/a"}]},
    {**B["trusted"], "commands": [{"prefix": "git", "mode": "allow"}, {"prefix": "git ", "mode": "deny"}]},
    {**B["trusted"], "commands": [{"prefix": "a\nb", "mode": "allow"}]},
    {**B["trusted"], "commands": [{"prefix": "   ", "mode": "allow"}]},
    {**B["trusted"], "commands": [{"prefix": "x"}]}]
for raw in invalid:
    try:
        sp.validate(raw)
    except BridgeError:
        continue
    sys.exit(f"expected invalid: {raw}")

globs = [{"pattern": p, "path": q, "match": sp.glob_matches(p, q)} for p, q in [
    ("*.pem", "keys/server.pem"), ("*.pem", "server.pem.bak"), ("secrets/**", "secrets/a/b"),
    ("secrets/**", "app/secrets/a"), ("secrets", "app/secrets/a"), ("build/", "build/out.js"),
    ("src/*.py", "src/a.py"), ("src/*.py", "src/sub/a.py"), ("**/gen", "a/b/gen/x"),
    ("data?.csv", "data1.csv"), ("data?.csv", "data10.csv"), ("[!a]*.txt", "b.txt"),
    ("[!a]*.txt", "a.txt"), ("[ab].md", "b.md"), ("x", ""), ("a.b", "aXb")]]

fixed = [{"path": p, "protected": sp.fixed_protected(p) is not None} for p in [
    ".git/config", "src/.git/HEAD", ".env", "app/.env.local", ".env.example", ".env.sample",
    ".env.template", ".envrc", "checks.toml", "sub/checks.toml", "src/main.py", ".github/ci.yml"]]

profiles = {"read-only": B["read-only"], "reviewed": B["reviewed"], "trusted": B["trusted"],
            "custom": custom, "locked": locked}
profiles = {name: sp.validate(rules) for name, rules in profiles.items()}

commands = []
for name, cmds in {
    "read-only": ["ls", "ls -la", "cat x && ls"],
    "locked": ["ls", "ls -la", "cat x && ls", "lsof"],
    "reviewed": ["ls", "", "   ", "a\x00b", "x" * 16385],
    "trusted": ["rm -rf build", "FOO=1 make"],
    "custom": ["git status", "git push origin main", "cd app && git push", "git  status  -s",
               "npm test", "npm test && git status", "npm test; make", "rm -rf x", "FOO=1 git push",
               "echo $(git push)", "git pushx", "make | tee log", "npm testing"],
}.items():
    for c in cmds:
        mode, _ = sp.command_mode(profiles[name], c)
        commands.append({"profile": name, "command": c, "mode": mode})

tmp = tempfile.mkdtemp()
ws, out = os.path.join(tmp, "ws"), os.path.join(tmp, "out")
for d in (ws + "/src", ws + "/secrets", out + "/shared/deep"):
    os.makedirs(d)
os.symlink("../out", ws + "/link_out")
os.symlink(".", ws + "/self")
def fill(rules):
    return json.loads(json.dumps(rules).replace("/@out", os.path.realpath(out)))
paths = []
for name, raws in {
    "read-only": ["src/a.py", ".", "/@out/x", ".env", ".git/config"],
    "reviewed": ["src/a.py", "src/new/file.py", "/@out/x", "link_out/x", "self/src/a.py",
                 "link_out/../ws/src/a.py", "src/../../out/x", "~/x", "~nobody/x", "", "a\x00b",
                 "checks.toml", "src/checks.toml", "/@out/.env", "/@out/repo/.git/HEAD"],
    "custom": ["secrets/key", "keys/a.pem", "build/x", "/@out/shared/deep/f", "/@out/shared",
               "/@out/other", "src/a.py"],
}.items():
    for raw in raws:
        for write in (False, True):
            target = raw.replace("/@out", os.path.realpath(out))
            mode, _ = sp.path_mode(fill(profiles[name]), ws, target, write=write)
            paths.append({"profile": name, "path": raw, "write": write, "mode": mode})

layout = {"dirs": ["ws/src", "ws/secrets", "out/shared/deep"],
          "symlinks": {"ws/link_out": "../out", "ws/self": "."}}
data = {"about": "Shared security profile vectors; generated from workspace_bridge.security_profiles "
                 "and checked by tests/test_security_profiles.py and the Pi policy tests. In paths "
                 "and profiles, /@out is the real path of the out directory of the layout.",
        "profiles": profiles, "canonical": canonical, "invalid": invalid, "globs": globs,
        "fixed": fixed, "commands": commands, "layout": layout, "paths": paths}
json.dump(data, open(sys.argv[1], "w"), indent=1, ensure_ascii=True)
print(len(canonical), len(invalid), len(globs), len(fixed), len(commands), len(paths))
