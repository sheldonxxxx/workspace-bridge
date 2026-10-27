"""Handoff documents and the prompts built from them.

Handoffs live in Bridge state. The agent's prompt and the manual-dispatch
prompt carry the full TASK, CONTEXT and ACCEPTANCE text, so nothing is
written into the workspace and nothing appears in git status. The Node also
keeps a read-only copy in its own state (``plan_folder``), which the prompt
names so an agent that lost its context can re-read the plan. Handoffs
published by earlier versions (``content`` NULL) still point at their
documents in the workspace's .workspace-handoff/ folder.
"""
from __future__ import annotations

import json
from pathlib import Path

from .security import HANDOFF

DOCUMENTS = ("TASK.md", "CONTEXT.md", "ACCEPTANCE.md")
PROMPT_DOCUMENT = "PROMPT.md"

_RULES = (
    "Preserve pre-existing edits and unrelated work. Follow the plan and satisfy every acceptance "
    "criterion. Run the agreed checks. Do not access secrets unless the handoff explicitly requires "
    "and authorizes it. Do not make unrelated destructive actions or expand scope unless the handoff "
    "explicitly permits them. Stop and report a blocker when assumptions fail or the work exceeds scope. ")

_MANUAL_REPORT = (
    "Do not weaken tests or invent results. Reply in your conversation with a concise summary: the "
    "handoff id, what changed and affected file paths, actual check commands and outcomes (including "
    "failures or checks not run), and remaining risks and blockers. The user will paste your reply into "
    "ChatGPT for review. No result files are required. Do not mark your own work audited, and do not "
    "commit, push or delete unrelated files unless the handoff explicitly permits it. Stop when complete.")


def documents(job: dict) -> dict[str, str] | None:
    """The stored document text, or None for a handoff kept in the workspace."""
    content = job.get("content")
    return json.loads(content) if content else None


def legacy_folder(root: str, job_id: str) -> str:
    return str(Path(root) / HANDOFF / "jobs" / job_id)


def _body(job: dict, docs: dict[str, str]) -> str:
    sections = "\n\n".join(f"===== {name} =====\n{docs[name].rstrip()}" for name in DOCUMENTS)
    folder = job.get("plan_folder")
    if folder:
        where = (f"The same documents are saved read-only in {json.dumps(folder)}. Re-read TASK.md, "
                 "CONTEXT.md and ACCEPTANCE.md there whenever you lose context and before you report. "
                 "Do not edit them.")
    else:
        where = "There are no handoff files to open."
    return (f"Handoff {job['id']}: its TASK, CONTEXT and ACCEPTANCE documents follow in full. "
            + where + "\n\n" + sections + "\n===== end of handoff =====\n")


def agent_prompt(root: str, job: dict) -> str:
    """The prompt a run starts with."""
    head = f"Work only in this project: {json.dumps(root)}. "
    docs = documents(job)
    if docs is None:
        folder = legacy_folder(root, job["id"])
        return (head + f"Read {json.dumps(folder + '/TASK.md')}, CONTEXT.md and ACCEPTANCE.md in the same "
                "folder. Do not edit the handoff documents. " + _RULES +
                "Report changed files, exact checks and outcomes, and remaining risks.")
    return (head + _RULES + "Report changed files, exact checks and outcomes, and remaining risks.\n\n"
            + _body(job, docs))


def manual_prompt(root: str, job: dict) -> str:
    """The prompt the user copies into a local agent themselves."""
    head = f"Work only in this project: {json.dumps(root)}. "
    docs = documents(job)
    if docs is None:
        folder = legacy_folder(root, job["id"])
        return (head + f"Read {json.dumps(folder + '/TASK.md')}, CONTEXT.md and ACCEPTANCE.md. "
                "Do not edit the handoff documents. " + _RULES + _MANUAL_REPORT)
    return head + _RULES + _MANUAL_REPORT + "\n\n" + _body(job, docs)
