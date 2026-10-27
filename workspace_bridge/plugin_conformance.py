"""Conformance kit for Agent Protocol v2 plugins.

``workspace-bridge plugin test <manifest>`` starts a plugin the way a Node
does, in a throwaway projects root, and drives it through the Bridge's own
strict client. Protocol checks never reach a model. Live checks (``--live``)
send a few short prompts to the real agent to prove idle-only starts, exact
interaction routing, restart recovery without replay, and that pushed events
agree with snapshots. A check that needs the agent to do something it did not
do is inconclusive rather than passed.
"""
from __future__ import annotations

import os
import secrets
import shutil
import signal
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from . import security_profiles as sp
from .adapter_events import validate_event_page
from .plugin_supervisor import PluginProcess
from .run_coordinator import _echo_matches
from .runtime import RuntimeRejected, RuntimeUnavailable, RuntimeUnsupported
from .security import BridgeError
from .wbrp import RuntimeClient

PROFILE_ID = "conformance"
# The command the interaction checks ask for; it writes, so even an agent
# with a read-only sandbox (Codex) must ask before it runs.
ASK_FILE = "wb-conformance.txt"
ASK_COMMAND = f"echo wb-conformance > {ASK_FILE}"
UNKNOWN_MODEL = "wb-conformance/no-such-model"
REPLY_PROMPT = "Reply with the single word {word} and nothing else. Do not use any tools."
ASK_PROMPT = ("Use your shell tool to run exactly this command, then reply with the word DONE: "
              + ASK_COMMAND)


class Fail(Exception):
    """The plugin broke the contract."""


class Skip(Exception):
    """The check does not apply to this plugin or this mode."""


class Inconclusive(Exception):
    """The agent never did what the check needs, so nothing was proven."""


def _rules() -> dict:
    # Trusted, plus one ask rule for the command the interaction checks use.
    return sp.validate({**sp.BUILTINS["trusted"],
                        "commands": [{"prefix": "echo wb-conformance", "mode": "ask"}]})


def _refused(call: Callable[[], Any], what: str) -> str:
    """Expect a 4xx refusal; return its code."""
    try:
        call()
    except RuntimeRejected as exc:
        return exc.code
    except (RuntimeUnavailable, RuntimeUnsupported) as exc:
        raise Fail(f"{what} failed as an outage ({exc.code}) instead of a refusal") from None
    raise Fail(f"{what} was accepted")


class Kit:
    def __init__(self, manifest: dict, *, settings: dict | None = None,
                 model: str | None = None, live: bool = False, timeout: float = 180,
                 keep: bool = False, progress: Callable[[dict], None] | None = None):
        self.manifest = manifest
        self.settings = dict(settings or {})
        self.model = model
        self.live = live
        self.timeout = timeout
        self.keep = keep
        self.progress = progress
        self.results: list[dict] = []
        self.base = Path(tempfile.mkdtemp(prefix="wb-conformance-")).resolve()
        self.projects = self.base / "projects"
        self.workspace = self.projects / "conformance"
        self.workspace.mkdir(parents=True)
        (self.workspace / "README.md").write_text("Workspace Bridge conformance workspace.\n")
        self.adapter_id = "adapter_" + secrets.token_hex(12)
        self.workspace_id = "ws_" + secrets.token_hex(12)
        self.process = PluginProcess(self.adapter_id, manifest, self.base / "plugin",
                                     projects_root=str(self.projects), settings=self.settings,
                                     log_level=self.settings.get("log_level"))
        self.client = RuntimeClient(self.adapter_id, manifest["runtime_type"], self.process,
                                    timeout=60)
        self.rules = _rules()
        self.security = sp.wire(PROFILE_ID, self.rules)
        self.descriptor = None
        self.conversation: dict | None = None
        self.runs: dict[str, dict] = {}       # native run id -> last snapshot
        self.selector: str | None = None
        self.waiting: dict | None = None      # run held on an interaction

    # -- harness ------------------------------------------------------------

    def check(self, name: str, fn: Callable[[], str | None]) -> bool:
        try:
            detail, status = fn() or "", "pass"
        except Skip as exc:
            detail, status = str(exc), "skip"
        except Inconclusive as exc:
            detail, status = str(exc), "inconclusive"
        except Fail as exc:
            detail, status = str(exc), "fail"
        except BridgeError as exc:
            detail, status = f"{exc.code}: {exc}", "fail"
        result = {"check": name, "status": status, "detail": detail[:300]}
        self.results.append(result)
        if self.progress is not None:
            self.progress(result)
        return status == "pass"

    def _payload(self, text: str, model: str | None = None) -> dict:
        payload = {"input": [{"type": "text", "text": text}],
                   "clientRunId": "run_" + secrets.token_hex(12), "security": self.security}
        model = model if model is not None else self.selector
        if model:
            payload["model"] = model
        return payload

    def _start(self, payload: dict) -> dict:
        run = self.client.start_run(self.conversation["id"], payload)
        if (run.get("clientRunId") != payload["clientRunId"]
                or run.get("conversationId") != self.conversation["id"]
                or not _echo_matches(self.security, run.get("securityBinding"))):
            raise Fail("Run did not echo its client run id, conversation and security binding")
        self.runs[run["id"]] = run
        return run

    def _wait(self, run_id: str, done: Callable[[dict], bool]) -> dict:
        deadline = time.monotonic() + self.timeout
        while True:
            run = self.client.run(run_id)
            self.runs[run_id] = run
            if done(run) or time.monotonic() > deadline:
                return run
            time.sleep(0.5)

    def _need_conversation(self) -> None:
        if self.conversation is None:
            raise Skip("No conversation was created")

    def _restart(self) -> None:
        process = self.process._process
        if process is None:
            raise Fail("Plugin is not running")
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(10)
        # The supervisor notices the crash on the next call and backs off
        # before it starts the plugin again.
        deadline = time.monotonic() + 30
        while True:
            try:
                self.descriptor = self.client.descriptor()
                return
            except RuntimeUnavailable:
                if time.monotonic() > deadline:
                    raise Fail("The plugin did not come back after a restart") from None
                time.sleep(0.5)

    # -- protocol checks (no model calls) ------------------------------------

    def check_descriptor(self) -> str:
        self.descriptor = self.client.descriptor()
        optional = sorted(set(self.descriptor.features) - {"models", "conversations", "runs",
                                                            "activities", "interactions"})
        return (f"{self.descriptor.display_name} {self.descriptor.adapter_version}; "
                f"optional features: {', '.join(optional) or 'none'}")

    def check_unknown_method(self) -> str:
        try:
            self.process.call("wb.conformance.unknown", {}, timeout=10)
        except RuntimeRejected:
            pass
        except RuntimeUnavailable:
            raise Fail("Unknown method was not answered with a refusal") from None
        else:
            raise Fail("Unknown method was answered as a success")
        self.client.descriptor()
        return "refused, plugin still answers"

    def check_models(self) -> str:
        raw = self.process.call("models", {"workspaceId": self.workspace_id}, timeout=60)
        rows = raw.get("models")
        if not isinstance(rows, list) or not rows:
            raise Fail("Model catalog is empty or invalid")
        selectors = [row.get("selector") for row in rows if isinstance(row, dict)]
        if len(selectors) != len(rows) or any(not isinstance(s, str) or not s or len(s) > 260
                                               for s in selectors):
            raise Fail("Every model needs a non-empty selector of at most 260 characters")
        if len(set(selectors)) != len(selectors):
            raise Fail("Model selectors are not unique")
        defaults = [row["selector"] for row in rows if row.get("default") is True]
        if len(defaults) > 1:
            raise Fail("More than one model is marked default")
        self.client.models(self.workspace_id)
        if self.model is not None and self.model not in selectors:
            raise Fail(f"--model {self.model} is not in the catalog")
        self.selector = self.model or (defaults[0] if defaults else None)
        return f"{len(selectors)} models; runs use {self.selector or 'the native default'}"

    def check_security_refused(self) -> str:
        tampered = sp.wire(PROFILE_ID, self.rules)
        tampered["profile"]["rules"] = {**self.rules, "shell": "deny"}
        codes = [_refused(lambda: self.client.create_conversation({
            "workspaceId": self.workspace_id, "directory": str(self.workspace),
            "security": tampered}), "A profile whose revision does not match its rules")]
        codes.append(_refused(lambda: self.client.create_conversation({
            "workspaceId": self.workspace_id, "directory": str(self.workspace)}),
            "A conversation without a security block"))
        if "native_security" not in self.manifest["features"]:
            codes.append(_refused(lambda: self.client.create_conversation({
                "workspaceId": self.workspace_id, "directory": str(self.workspace),
                "security": {"source": "native"}}),
                "Native security from a plugin without native_security"))
        return "refused: " + ", ".join(codes)

    def check_root(self) -> str:
        outside = self.base / "outside"
        outside.mkdir(exist_ok=True)
        code = _refused(lambda: self.client.create_conversation({
            "workspaceId": self.workspace_id, "directory": str(outside),
            "security": self.security}), "A directory outside the projects root")
        return f"refused: {code}"

    def check_conversation(self) -> str:
        missing = sp.unenforced(self.rules, self.manifest["enforces"])
        if missing:
            raise Skip("The plugin does not enforce " + ", ".join(missing)
                       + ", which the kit's profile restricts")
        conversation = self.client.create_conversation({
            "workspaceId": self.workspace_id, "directory": str(self.workspace),
            "security": self.security})
        if (conversation.get("workspaceId") != self.workspace_id
                or not isinstance(conversation.get("id"), str) or not conversation["id"]
                or not _echo_matches(self.security, conversation.get("securityBinding"))):
            raise Fail("Conversation did not echo its workspace and security binding")
        self.conversation = conversation
        current = self.client.conversation(conversation["id"])
        if current.get("workspaceId") != self.workspace_id or current.get("status") != "idle":
            raise Fail("A new conversation must read back idle in its workspace")
        return f"created {conversation['id']}, security echoed"

    def check_ownership(self) -> str:
        foreign = "conv_" + secrets.token_hex(12)
        codes = [_refused(lambda: self.client.conversation(foreign),
                          "Reading a conversation this plugin never created")]
        codes.append(_refused(lambda: self.client.start_run(foreign, self._payload("Hi")),
                              "Starting a run in a conversation this plugin never created"))
        codes.append(_refused(lambda: self.client.run("run_" + secrets.token_hex(12)),
                              "Reading an unknown run"))
        codes.append(_refused(lambda: self.client.resolve("int_" + secrets.token_hex(12),
                                                          {"choiceId": "x"}),
                              "Answering an unknown interaction"))
        return "refused: " + ", ".join(codes)

    # -- live checks ---------------------------------------------------------

    def _live(self) -> None:
        if not self.live:
            raise Skip("Needs --live (sends short prompts to the agent)")
        self._need_conversation()

    def check_run(self) -> str:
        self._live()
        payload = self._payload(REPLY_PROMPT.format(word="READY"))
        run = self._start(payload)
        again = self.client.start_run(self.conversation["id"], payload)
        if again["id"] != run["id"]:
            raise Fail("Repeating a start with the same client run id started a second run")
        found = self.client.find_run(self.conversation["id"], payload["clientRunId"])
        if found["id"] != run["id"]:
            raise Fail("Finding a run by its client run id returned another run")
        run = self._wait(run["id"], lambda r: r["phase"] == "terminal")
        if run["phase"] != "terminal":
            raise Fail(f"Run did not finish within {self.timeout:.0f}s")
        if run["outcome"] != "succeeded":
            raise Fail(f"Run ended {run['outcome']}: {str(run.get('error') or '')[:200]}")
        for activity in self.client.activities(run["id"]):
            if activity.get("runId") not in (None, run["id"]):
                raise Fail("An activity names another run")
            if isinstance(activity.get("id"), str):
                self.client.activity(activity["id"])
        if self.client.conversation(self.conversation["id"]).get("status") != "idle":
            raise Fail("Conversation is not idle after its run finished")
        return "succeeded; a repeated start returned the same run"

    def check_unknown_model(self) -> str:
        self._live()
        try:
            run = self._start(self._payload(REPLY_PROMPT.format(word="NO"), UNKNOWN_MODEL))
        except RuntimeRejected as exc:
            return f"refused at start: {exc.code}"
        run = self._wait(run["id"], lambda r: r["phase"] == "terminal")
        if run["phase"] != "terminal":
            self.client.cancel(run["id"])
            raise Fail("A run on an unknown model did not finish")
        if run["outcome"] == "succeeded":
            raise Fail("A run on an unknown model succeeded; selectors must not be remapped")
        return f"run ended {run['outcome']}"

    def check_events(self) -> str:
        self._live()
        if self.descriptor is None or not self.descriptor.supports("events"):
            raise Skip("Plugin does not advertise events")
        after, events = 0, []
        for _ in range(50):
            page = validate_event_page(self.client.events(after=after, wait_ms=0), after)
            if page["instanceId"] != self.descriptor.instance_id:
                raise Fail("Event instance id differs from the descriptor's")
            events.extend(page["events"])
            if not page["events"]:
                break
            after = page["events"][-1]["cursor"]
        if not events:
            raise Fail("No events were pushed for the runs")
        for event in events:
            if event["conversationId"] != self.conversation["id"]:
                raise Fail("An event names a conversation the kit never created")
            if event["runId"]:
                snapshot = self.client.run(event["runId"])
                if snapshot.get("conversationId") != event["conversationId"]:
                    raise Fail("An event's run belongs to another conversation in its snapshot")
        seen = {event["runId"] for event in events if event["runId"]}
        if not set(self.runs) <= seen:
            raise Fail("Some runs produced no event")
        return f"{len(events)} events agree with snapshots"

    def check_interaction(self) -> str:
        self._live()
        run = self._start(self._payload(ASK_PROMPT))
        run = self._wait(run["id"], lambda r: r["phase"] == "terminal"
                         or r.get("activeState") == "waiting_interaction")
        if run["phase"] != "active" or run.get("activeState") != "waiting_interaction":
            if run["phase"] != "terminal":
                self.client.cancel(run["id"])
            if (self.workspace / ASK_FILE).exists():
                raise Fail("The agent ran a command its profile asks about without asking")
            raise Inconclusive("The agent never asked to run the command; rerun the kit")
        pending = self.client.interactions(run["id"])
        if not pending:
            raise Fail("Run waits on an interaction but lists none")
        for item in pending:
            if item.get("runId") != run["id"] or item.get("kind") not in ("choice", "grant", "form"):
                raise Fail("An interaction does not name its run and a known kind")
            if item.get("state") != "pending":
                raise Fail("A listed interaction is not pending")
        interaction = pending[0]
        if interaction["kind"] == "choice" and not interaction.get("choices"):
            raise Fail("A choice interaction offers no choices")
        _refused(lambda: self.client.resolve(interaction["id"], {"choiceId": "not-a-choice"}),
                 "An answer with a choice the interaction does not offer")
        busy = self._payload(REPLY_PROMPT.format(word="BUSY"))
        try:
            extra = self._start(busy)
        except RuntimeRejected as exc:
            self.waiting = {"run": run, "interaction": interaction}
            return f"command asked as {interaction['id']}; a second start was refused ({exc.code})"
        self.client.cancel(extra["id"])
        self.client.cancel(run["id"])
        raise Fail("A run started while the conversation was busy")

    def check_restart(self) -> str:
        self._live()
        if self.waiting is None:
            raise Skip("Needs a run waiting on an interaction")
        run, interaction = self.waiting["run"], self.waiting["interaction"]
        self._restart()
        after = self.client.run(run["id"])
        pending = self.client.interactions(run["id"])
        if any(item["id"] != interaction["id"] for item in pending):
            raise Fail("After restart the run asks again under a new interaction: "
                       "the prompt was replayed")
        if after["phase"] == "terminal":
            if after["outcome"] not in ("interrupted", "orphaned"):
                raise Fail(f"A run cut off by a restart ended {after['outcome']}")
            if pending:
                raise Fail("An interrupted run still lists a pending interaction")
            _refused(lambda: self.client.resolve(interaction["id"],
                                                 {"choiceId": self._deny(interaction)}),
                     "Answering the interaction of an interrupted run")
            settled = len(self.client.activities(run["id"]))
            time.sleep(3)
            if (len(self.client.activities(run["id"])) != settled
                    or self.client.run(run["id"])["phase"] != "terminal"):
                raise Fail("An interrupted run kept working after restart")
            detail = "run interrupted, its interaction refused"
        else:
            if not pending:
                raise Fail("A run that survived the restart lost its interaction")
            self.client.resolve(interaction["id"], {"choiceId": self._deny(interaction)})
            after = self._wait(run["id"], lambda r: r["phase"] == "terminal")
            if after["phase"] != "terminal":
                raise Fail("The run did not finish after its interaction was answered")
            _refused(lambda: self.client.resolve(interaction["id"],
                                                 {"choiceId": self._deny(interaction)}),
                     "Answering the same interaction twice")
            detail = "run rebound to the same interaction and finished once answered"
        if self.client.conversation(self.conversation["id"]).get("status") != "idle":
            raise Fail("The conversation is not idle after its run ended")
        return detail

    @staticmethod
    def _deny(interaction: dict) -> str:
        choices = interaction.get("choices") or []
        deny = next((c for c in choices if c.get("semantic") == "deny"), None)
        return str((deny or (choices[-1] if choices else {})).get("id") or "deny")

    def check_continuation(self) -> str:
        self._live()
        if self.waiting is None:
            # Still prove the plugin keeps its conversation across a restart.
            self._restart()
        run = self._start(self._payload(REPLY_PROMPT.format(word="AGAIN")))
        run = self._wait(run["id"], lambda r: r["phase"] == "terminal")
        if run["outcome"] != "succeeded":
            raise Fail(f"A run after restart ended {run['outcome'] or 'unfinished'}")
        return "the conversation survived a restart and ran again"

    # -- driver --------------------------------------------------------------

    def run_all(self) -> dict:
        try:
            if self.check("descriptor", self.check_descriptor):
                for name, fn in (("unknown_method", self.check_unknown_method),
                                 ("models", self.check_models),
                                 ("security_refused", self.check_security_refused),
                                 ("projects_root", self.check_root),
                                 ("conversation", self.check_conversation),
                                 ("ownership", self.check_ownership),
                                 ("run", self.check_run),
                                 ("unknown_model", self.check_unknown_model),
                                 ("interaction", self.check_interaction),
                                 ("events", self.check_events),
                                 ("restart", self.check_restart),
                                 ("continuation", self.check_continuation)):
                    self.check(name, fn)
        finally:
            self._cleanup()
        statuses = {result["status"] for result in self.results}
        return {"plugin": self.manifest["id"], "version": self.manifest["version"],
                "live": self.live, "passed": statuses <= {"pass", "skip"},
                "checks": self.results,
                **({"kept": str(self.base)} if self.keep else {})}

    def _cleanup(self) -> None:
        for run_id, run in list(self.runs.items()):
            if run.get("phase") != "terminal":
                try:
                    self.client.cancel(run_id)
                except BridgeError:
                    pass
        self.process.close()
        if not self.keep:
            shutil.rmtree(self.base, ignore_errors=True)


def run_kit(manifest: dict, **options) -> dict:
    return Kit(manifest, **options).run_all()
