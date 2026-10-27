"""Handoff documents and the prompts built from them.

Handoffs live in Bridge state. The agent's prompt and the manual-dispatch
prompt carry the full TASK, CONTEXT and ACCEPTANCE text, so nothing is
written into the workspace and nothing appears in git status. The Node also
keeps a read-only copy in its own state (``plan_folder``), with any uploaded
images under ``attachments/``, which the prompt names so an agent that lost
its context can re-read the plan.
"""
from __future__ import annotations

import json

from .security import BridgeError

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


# Handoffs 0.3 wrote into the workspace have no stored text.
UNSUPPORTED = ("This handoff was saved by 0.3 in the workspace folder, which is no longer read; "
               "save it again with save_handoff")


def documents(job: dict) -> dict[str, str]:
    """The stored document text."""
    content = job.get("content")
    if not content:
        raise BridgeError(UNSUPPORTED, "unsupported_handoff")
    return json.loads(content)


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


def _isolation(worktree: dict) -> str:
    return (f"This run works in a separate Git worktree on branch {json.dumps(worktree['branch'])}, made "
            "from the project's current files. Your changes reach the project only if ChatGPT applies them "
            "after review. Ignored files, such as installed dependencies and .env files, are not in the "
            "worktree; install what the checks need inside it, and do not touch the original project "
            "folder. Do not commit, switch branches or rewrite history. ")


def agent_prompt(root: str, job: dict, worktree: dict | None = None) -> str:
    """The prompt a run starts with; ``worktree`` names an isolated run's own checkout."""
    project = worktree["path"] if worktree is not None else root
    head = f"Work only in this project: {json.dumps(project)}. "
    if worktree is not None:
        head += _isolation(worktree)
    docs = documents(job)
    return (head + _RULES + "Report changed files, exact checks and outcomes, and remaining risks.\n\n"
            + _body(job, docs))


def manual_prompt(root: str, job: dict) -> str:
    """The prompt the user copies into a local agent themselves."""
    head = f"Work only in this project: {json.dumps(root)}. "
    docs = documents(job)
    return head + _RULES + _MANUAL_REPORT + "\n\n" + _body(job, docs)
