import json
import os

import pytest

from workspace_bridge.api import Grep
from workspace_bridge.browse import glob_matcher
from workspace_bridge.security import BridgeError, MAX_FILE, MAX_OUTPUT, redact


def call(env, tool, **args):
    if tool == "grep_files":
        args = Grep.model_validate(args).model_dump()
    return env["service"].call(env["id"], env["token"], tool, args)


def test_list_dir_includes_empty_folders_and_respects_depth(env):
    (env["root"] / "empty").mkdir()
    (env["root"] / "src/deep").mkdir()
    (env["root"] / "src/deep/deeper.py").write_text("pass\n")
    first = call(env, "list_dir")
    entries = {e["path"]: e for e in first["entries"]}
    assert entries["src"]["type"] == "directory" and entries["empty"]["type"] == "directory"
    assert entries["README.md"]["type"] == "file" and "src/main.py" not in entries
    second = call(env, "list_dir", depth=2)
    names = {e["path"] for e in second["entries"]}
    assert "src/main.py" in names and "src/deep" in names and "src/deep/deeper.py" not in names


def test_list_dir_paging_and_stale_listing(env):
    first = call(env, "list_dir", depth=2, limit=1)
    second = call(env, "list_dir", depth=2, offset=first["next_offset"], limit=1,
                  expected_listing_sha256=first["listing_sha256"])
    assert first["entries"] != second["entries"]
    (env["root"] / "another.py").write_text("pass")
    with pytest.raises(BridgeError, match="Listing changed"):
        call(env, "list_dir", depth=2, offset=1, expected_listing_sha256=first["listing_sha256"])


@pytest.mark.parametrize("pattern,path,expected", [
    ("**/*.py", "main.py", True), ("**/*.py", "src/deep/main.py", True),
    ("*.py", "src/main.py", False), ("src/*.py", "src/main.py", True),
    ("src/*.py", "src/deep/main.py", False), ("src/**/*.py", "src/main.py", True),
    ("**/*", "README.md", True), ("file?.[ch]", "file1.c", True), ("file?.[ch]", "file1.py", False),
])
def test_glob_semantics(pattern, path, expected):
    assert glob_matcher(pattern)(path) is expected


@pytest.mark.parametrize("pattern", ["../*", "/etc/*", "src//*.py", "a/**b", "C:\\*", "*.{py,js}"])
def test_invalid_globs_fail_closed(pattern):
    with pytest.raises(BridgeError):
        glob_matcher(pattern)


def test_glob_path_filter_and_admin_exclusions(env):
    (env["root"] / "root.py").write_text("pass")
    (env["root"] / "src/private.py").write_text("sensitive")
    env["service"].manage_workspace(env["id"], "set_excludes", ["src/private.py"])
    result = call(env, "glob", path="src", pattern="**/*.py")
    assert [x["path"] for x in result["entries"]] == ["src/main.py"]
    assert {x["path"] for x in call(env, "glob", pattern="**/*.py")["entries"]} == {"root.py", "src/main.py"}


@pytest.mark.parametrize("tool,args", [("list_dir", {"depth": 4}), ("glob", {"pattern": "**/*"}),
                                      ("grep_files", {"pattern": "sensitive"})])
def test_all_browse_tools_exclude_secrets_links_and_special_files(env, tool, args):
    secret = env["tmp"] / "outside"; secret.write_text("sensitive")
    (env["root"] / "link.txt").symlink_to(secret)
    os.link(secret, env["root"] / "hard.txt")
    os.mkfifo(env["root"] / "pipe")
    (env["root"] / ".env").write_text("sensitive")
    (env["root"] / ".ignore").write_text("!.env\n!link.txt\n")
    result = call(env, tool, **args)
    body = json.dumps(result.get("entries", result.get("matches")))
    assert all(name not in body for name in [".env", "link.txt", "hard.txt", "pipe"])


def test_grep_regex_include_context_and_case(env):
    (env["root"] / "src/feature.py").write_text("# before\ndef FEATURE(x):\n    return x\n")
    r = call(env, "grep_files", pattern=r"^def\s+feature\(", include="**/*.py", context_lines=1)
    assert len(r["matches"]) == 1 and r["matches"][0]["line"] == 2
    assert [x["line"] for x in r["matches"][0]["context"]] == [1, 2, 3]
    assert not call(env, "grep_files", pattern="feature", include="**/*.py", case_sensitive=True)["matches"]
    assert not call(env, "grep_files", pattern="FEATURE", include="**/*.md")["matches"]


def test_grep_literal_mode(env):
    (env["root"] / "literal.txt").write_text("a.b\nacb\n")
    literal = call(env, "grep_files", pattern="a.b", include="*.txt", fixed_strings=True)
    regex = call(env, "grep_files", pattern="a.b", include="*.txt")
    assert len(literal["matches"]) == 1 and len(regex["matches"]) == 2


def test_grep_invalid_regex(env):
    with pytest.raises(BridgeError, match="Invalid regular"):
        call(env, "grep_files", pattern="[")


def test_grep_timeout_is_bounded(env):
    (env["root"] / "adversarial.txt").write_text("a" * 100000 + "!")
    with pytest.raises(BridgeError, match="time budget"):
        call(env, "grep_files", pattern=r"(a+)+$", include="*.txt")


def test_grep_pagination_no_missing_or_duplicate_lines(env):
    for name in ["a.txt", "b.txt"]:
        (env["root"] / name).write_text("\n".join("match " + str(i) for i in range(17)))
    cursor, observed = None, []
    for _ in range(30):
        result = call(env, "grep_files", pattern="match", include="*.txt", limit=3, cursor=cursor)
        observed += [(x["path"], x["line"]) for x in result["matches"]]
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert observed == [(name, i) for name in ["a.txt", "b.txt"] for i in range(1, 18)]
    assert result["search_complete"]


def test_grep_empty_page_is_not_false_completion(env):
    for n in range(101):
        (env["root"] / f"f{n:03}.txt").write_text("target" if n == 100 else "other")
    result = call(env, "grep_files", pattern="target", include="*.txt")
    assert not result["matches"] and result["next_cursor"] and not result["search_complete"]
    continued = call(env, "grep_files", pattern="target", include="*.txt", cursor=result["next_cursor"])
    assert continued["matches"][0]["path"] == "f100.txt" and continued["search_complete"]


def test_grep_cursor_rejects_tampering_changed_query_and_other_workspace(env):
    (env["root"] / "x.txt").write_text("match\nmatch\n")
    cursor = call(env, "grep_files", pattern="match", limit=1)["next_cursor"]
    assert cursor
    for args in [{"pattern": "different", "cursor": cursor}, {"pattern": "match", "cursor": "X" + cursor[1:]}]:
        with pytest.raises(BridgeError, match="Invalid cursor"):
            call(env, "grep_files", **args)
    root = env["parent"] / "beta"; root.mkdir(); (root / "x.txt").write_text("match\nmatch\n")
    other = env["service"].add_workspace("Beta", str(root), [])["workspace"]["id"]
    env["service"].manage_workspace(other, "enable")
    with pytest.raises(BridgeError, match="Invalid cursor"):
        env["service"].call(other, env["token"], "grep_files", Grep.model_validate({"pattern": "match", "cursor": cursor}).model_dump())


def test_grep_cursor_rejects_same_size_edits(env):
    p = env["root"] / "x.txt"; p.write_text("match\nmatch\n")
    first = call(env, "grep_files", pattern="match", limit=1)
    p.write_text("other\nother\n")
    with pytest.raises(BridgeError, match="inventory changed"):
        call(env, "grep_files", pattern="match", cursor=first["next_cursor"])


def test_grep_reports_skipped_binary_and_oversize(env):
    (env["root"] / "binary.txt").write_bytes(b"x\x00target")
    (env["root"] / "large.txt").write_bytes(b"a" * (MAX_FILE + 1))
    result = call(env, "grep_files", pattern="target", include="*.txt")
    assert not result["matches"] and result["skipped_this_page"] == 2
    assert {x["reason"] for x in result["skipped_files"]} == {"binary_or_non_utf8", "too_large"}


def test_multiline_redaction_preserves_line_numbers(env):
    text = "first\n-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----\nafter\n"
    result, redacted = redact(text)
    assert redacted and result.count("\n") == text.count("\n") and "secret" not in result
    (env["root"] / "redact.txt").write_text(text)
    grep = call(env, "grep_files", pattern="after", include="*.txt")
    assert grep["matches"][0]["line"] == 5
    read = call(env, "read_file", path="redact.txt", start_line=5, max_lines=1, expected_sha256=None)
    assert read["lines"] == [{"line": 5, "text": "after"}]


def test_grep_redacts_before_search_and_does_not_log_pattern(env):
    (env["root"] / "auth.py").write_text('password = "verysecret123"\n')
    result = call(env, "grep_files", pattern="verysecret123")
    assert not result["matches"]
    rows = [dict(x) for x in env["service"].db.execute("SELECT * FROM events")]
    assert "verysecret123" not in json.dumps(rows)


def test_grep_output_budget_continues_without_losing_large_matches(env):
    for n in range(9):
        (env["root"] / (f"{n}-" + "a" * 200 + ".txt")).write_text("\n".join("match" + "x" * 900 for _ in range(5)))
    cursor, observed = None, set()
    for _ in range(100):
        result = call(env, "grep_files", pattern="match", include="*.txt", context_lines=5, limit=100, cursor=cursor)
        assert len(json.dumps(result, ensure_ascii=False).encode()) < MAX_OUTPUT
        for m in result["matches"]:
            item = (m["path"], m["line"])
            assert item not in observed
            observed.add(item)
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert len(observed) == 45
