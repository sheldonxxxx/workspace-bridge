"""Package-owned project-lead guidance; never loaded from a mapped workspace."""
from __future__ import annotations

from hashlib import sha256
from importlib.resources import files

SKILL_NAME = "project-lead"
SKILL_VERSION = "2.1.0"
SKILL_TOOL = "read_project_lead_skill"


def skill_hint() -> dict[str, str]:
    """Small discovery pointer; do not repeat the full skill on every call."""
    return {"name": SKILL_NAME, "version": SKILL_VERSION, "read_tool": SKILL_TOOL}


def read_project_lead_skill() -> dict[str, str]:
    """Read the one bundled skill. No caller-controlled path or workspace access."""
    content = (files("workspace_bridge") / "skills" / SKILL_NAME / "SKILL.md").read_text(encoding="utf-8")
    return {"name": SKILL_NAME, "version": SKILL_VERSION,
            "sha256": sha256(content.encode("utf-8")).hexdigest(), "content": content}
