"""Package-owned project-lead guidance; never loaded from a mapped workspace.

Served through the MCP Skills extension (``skills/list``, ``skills/get`` and
``resources/read`` on ``skill://`` URIs), not as a tool.
"""
from __future__ import annotations

from hashlib import sha256
from importlib.resources import files

SKILL_NAME = "project-lead"
SKILL_VERSION = "3.3.0"
SKILLS_EXTENSION = "io.modelcontextprotocol/skills"
SKILL_URI = f"skill://workspace-bridge/{SKILL_NAME}/SKILL.md"


def skill_hint() -> dict[str, str]:
    """Small discovery pointer; do not repeat the full skill on every call."""
    return {"name": SKILL_NAME, "version": SKILL_VERSION, "uri": SKILL_URI}


def read_project_lead_skill() -> dict[str, str]:
    """Read the one bundled skill. No caller-controlled path or workspace access."""
    content = (files("workspace_bridge") / "skills" / SKILL_NAME / "SKILL.md").read_text(encoding="utf-8")
    return {"name": SKILL_NAME, "version": SKILL_VERSION,
            "sha256": sha256(content.encode("utf-8")).hexdigest(), "content": content}


def _frontmatter(content: str) -> dict[str, str]:
    lines = content.split("\n")
    if not lines or lines[0] != "---":
        raise ValueError("SKILL.md requires frontmatter")
    end = lines.index("---", 1)
    result: dict[str, str] = {}
    for line in lines[1:end]:
        key, _, text = line.partition(":")
        result[key.strip()] = text.strip()
    return result


def skill_entry() -> dict:
    """``skills/list`` / ``skills/get`` entry: URI, parsed frontmatter, digests."""
    content = read_project_lead_skill()["content"]
    return {"uri": SKILL_URI, "frontmatter": _frontmatter(content),
            "resources": [{"uri": SKILL_URI,
                           "digest": "sha256:" + sha256(content.encode("utf-8")).hexdigest()}]}


def read_skill_resource(uri: str) -> dict | None:
    """``resources/read`` result for the one bundled URI; ``None`` when unknown."""
    if uri != SKILL_URI:
        return None
    return {"contents": [{"uri": SKILL_URI, "mimeType": "text/markdown",
                          "text": read_project_lead_skill()["content"]}]}
