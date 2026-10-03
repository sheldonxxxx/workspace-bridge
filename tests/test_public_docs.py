"""Public documentation surface: links resolve, working notes stay out.

Proves the public docs consolidation: no tracked public Markdown links to
removed first-party docs, and no internal working markers leak into tracked
public docs. Scans the repo checkout only; never `.workspace-handoff/` or
vendored `.agents/skills` content.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PUBLIC_DOCS = [
    REPO / "README.md",
    REPO / "CHANGELOG.md",
    REPO / "AGENTS.md",
    REPO / "CONTRIBUTING.md",
    REPO / "runtime" / "pi-host-adapter" / "README.md",
    REPO / "web" / "README.md",
    *sorted((REPO / "docs").glob("*.md")),
]

REMOVED_DOCS = (
    "CHATGPT_INSTRUCTIONS.md",
    "HANDOFF_WRITES.md",
    "TUNNEL_SETUP.md",
    "CODEX_ADAPTER.md",
)

# Internal working markers that must not appear in tracked public docs.
MARKERS = (
    "M4.",
    "M4_",
    "start_opencode",
    "start_pi.sh",
    "com.workspace-bridge.pi-host-adapter",
    "deploy plan",
    "sync_adapters_local",
    "next milestone",
    "Next milestone",
)

# `agent_enabled` / OpenCode history may survive only in code identifiers and
# tests, never in public prose.
PROSE_MARKERS = ("agent_enabled", "OpenCode", "opencode")


def _public_texts() -> dict[Path, str]:
    texts = {}
    for path in PUBLIC_DOCS:
        assert path.is_file(), f"expected public doc missing: {path}"
        texts[path] = path.read_text(encoding="utf-8")
    return texts


def test_no_links_to_removed_docs():
    texts = _public_texts()
    link_re = re.compile(r"\]\(([^)#\s]+\.md)(?:#[^)\s]*)?\)")
    failures = []
    for path, text in texts.items():
        for match in link_re.finditer(text):
            target = match.group(1)
            name = target.rsplit("/", 1)[-1]
            if name in REMOVED_DOCS:
                failures.append(f"{path.name} links to removed {name}")
        for name in REMOVED_DOCS:
            if name in text and "](" not in text[max(0, text.find(name) - 60):text.find(name) + len(name)]:
                # Bare filename mentions outside links also fail; docs must
                # not direct readers to deleted files.
                failures.append(f"{path.name} mentions removed {name}")
    assert not failures, "\n".join(failures)


def test_no_internal_working_markers_in_public_docs():
    texts = _public_texts()
    failures = []
    for path, text in texts.items():
        for marker in MARKERS:
            if marker in text:
                failures.append(f"{path.name} contains {marker!r}")
        for marker in PROSE_MARKERS:
            if marker in text:
                failures.append(f"{path.name} contains {marker!r}")
    assert not failures, "\n".join(failures)


def test_no_false_journal_absolute():
    # Support bundles intentionally collect bounded sanitized journal
    # excerpts, so no public doc may claim journals are never read.
    texts = _public_texts()
    failures = [
        str(path) for path, text in texts.items()
        if "never scrapes journal" in text
    ]
    assert not failures, "\n".join(failures)


def test_mcp_tools_matches_run_contract():
    text = (REPO / "docs" / "MCP_TOOLS.md").read_text(encoding="utf-8")
    assert "instruction" in text, "start_agent_run must document direct instruction"
    assert "exactly one of" in text, "start_agent_run must state the job_id/instruction choice"
    assert "per-route" in text, "execution semantics must be per-route, not workspace-wide"


def test_agent_setup_covers_both_runtimes():
    text = (REPO / "docs" / "AGENT_SETUP.md").read_text(encoding="utf-8")
    assert "--runtime pi" in text
    assert "--runtime codex" in text
    assert "workspace-bridge-adapter-pi" in text and "service install" in text
    assert "workspace-bridge-adapter-codex" in text
    codex_block = text[text.find("--runtime codex"):]
    assert "service install" in codex_block and "service status" in codex_block


def test_no_apt_installs_uv():
    # Runbooks must not assume an apt package for uv; they point at
    # Astral-documented installation methods instead.
    apt_uv = re.compile(r"apt(-get)?\s+(update[^\n]*?&&\s+)?install\b[^\n]*\buv\b")
    texts = _public_texts()
    failures = []
    for path, text in texts.items():
        for line in text.splitlines():
            if apt_uv.search(line):
                failures.append(f"{path.name}: {line.strip()}")
    assert not failures, "\n".join(failures)


def test_readme_quick_start_registers_node_first():
    text = (REPO / "README.md").read_text(encoding="utf-8")
    section = text[text.find("## Quick start"):]
    assert "show-token" in section, "quick start must show Node token retrieval"
    assert "register" in section.lower(), "quick start must cover Node registration"
    assert section.find("show-token") < section.find("mapping"), \
        "Node token/registration must precede workspace mapping"


def test_deleted_docs_stay_deleted():
    for name in REMOVED_DOCS:
        assert not (REPO / "docs" / name).exists(), f"{name} was restored"
    assert not (REPO / "start_pi.sh").exists(), "start_pi.sh was restored"


def test_canonical_docs_exist():
    for name in (
        "README.md",
        "AGENT_SETUP.md",
        "RUNTIMES.md",
        "SETUP.md",
        "ARCHITECTURE.md",
        "OPERATIONS.md",
    ):
        if name == "README.md" and (REPO / "docs" / name).exists():
            continue
        candidates = [REPO / name, REPO / "docs" / name]
        assert any(p.is_file() for p in candidates), f"missing {name}"
    assert (REPO / "CONTRIBUTING.md").is_file()
