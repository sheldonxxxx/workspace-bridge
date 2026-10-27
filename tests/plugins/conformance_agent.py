"""A scripted agent plugin that follows the whole contract, for the conformance kit.

A prompt containing ``echo wb-conformance`` asks for approval of that command;
any other prompt replies at once. State survives restarts in one JSON file.
The ``flaw`` setting breaks exactly one rule so tests can see the kit catch it.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from workspace_bridge import security_profiles as sp  # noqa: E402
from workspace_bridge.plugin_host import PluginServer  # noqa: E402
from workspace_bridge.security import BridgeError  # noqa: E402

MODEL = "fake/model-1"


class Failure(Exception):
    def __init__(self, message: str, status: int, code: str):
        super().__init__(message)
        self.status, self.code = status, code


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class ConformanceAgent:
    def __init__(self, params: dict):
        self.flaw = (params.get("settings") or {}).get("flaw", "")
        self.root = os.path.realpath(params["projectsRoot"])
        self.path = Path(params["state"]) / "agent.json"
        self.instance_id = _id("inst")
        self.lock = threading.Condition()
        self.cursor = 0
        self.events_buffer: list[dict] = []
        self.data = (json.loads(self.path.read_text()) if self.path.exists()
                     else {"conversations": {}, "runs": {}, "interactions": {}, "activities": {}})
        # Nothing survives a restart except what is on disk: an active run is
        # interrupted and its interaction goes stale. The replay flaw asks again.
        for run in self.data["runs"].values():
            if run["phase"] != "terminal":
                if self.flaw == "replay":
                    self._ask(run)
                    continue
                run.update(phase="terminal", activeState=None, outcome="interrupted")
                self.data["conversations"][run["conversationId"]]["status"] = "idle"
        for interaction in self.data["interactions"].values():
            run = self.data["runs"][interaction["runId"]]
            if interaction["state"] == "pending" and run["phase"] == "terminal":
                interaction["state"] = "stale"
        self._save()

    # -- helpers -------------------------------------------------------------

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.data))

    def _emit(self, kind: str, run: dict) -> None:
        conversation = run["conversationId"]
        if self.flaw == "events":
            conversation = "conv_somebody_else"
        self.cursor += 1
        self.events_buffer.append({"instanceId": self.instance_id, "cursor": self.cursor,
                                   "type": kind, "conversationId": conversation,
                                   "runId": run["id"]})
        self.lock.notify_all()

    def _security(self, body: dict) -> dict:
        block = body.get("security")
        if isinstance(block, dict) and block.get("source") == "native":
            raise Failure("Native security is unsupported", 400, "security_unavailable")
        try:
            ident, revision, _ = sp.from_wire(block)
        except BridgeError as exc:
            raise Failure(str(exc), 400, "invalid_profile") from None
        if self.flaw == "echo":
            revision = "prof_" + "0" * 32
        return {"source": "profile", "profile": {"id": ident, "revision": revision}}

    def _ask(self, run: dict) -> None:
        interaction = {"id": _id("int"), "runId": run["id"], "kind": "choice",
                       "state": "pending", "title": "Run a shell command",
                       "resource": "echo wb-conformance",
                       "choices": [{"id": _id("ch"), "label": "Approve", "semantic": "approve"},
                                   {"id": _id("ch"), "label": "Deny", "semantic": "deny"}]}
        self.data["interactions"][interaction["id"]] = interaction
        run.update(phase="active", activeState="waiting_interaction", outcome=None)

    def _finish(self, run_id: str, result: str) -> None:
        with self.lock:
            run = self.data["runs"][run_id]
            if run["phase"] == "terminal":
                return
            activity = {"id": _id("act"), "runId": run_id, "kind": "other",
                        "title": "Reply", "status": "completed"}
            self.data["activities"][activity["id"]] = activity
            run.update(phase="terminal", activeState=None, result=result,
                       outcome="failed" if result == "model unavailable" else "succeeded")
            self.data["conversations"][run["conversationId"]]["status"] = "idle"
            self._save()
            self._emit("run.completed", run)

    # -- protocol ------------------------------------------------------------

    def descriptor(self):
        return {"protocol": {"major": 1, "minor": 0},
                "runtime": {"id": "pi", "displayName": "Conformance", "adapterVersion": "1",
                            "nativeVersion": "1", "instanceId": self.instance_id},
                "features": {"models": 1, "conversations": 1, "runs": 1, "activities": 1,
                             "interactions": 1, "events": 1}}

    def models(self):
        return {"models": [{"selector": MODEL, "displayName": "Model one", "default": True}]}

    def create_conversation(self, body):
        binding = self._security(body)
        directory = os.path.realpath(str(body.get("directory", "")))
        if not directory.startswith(self.root + os.sep):
            raise Failure("Directory is outside the projects root", 400, "root_denied")
        with self.lock:
            conversation = {"id": _id("conv"), "workspaceId": body.get("workspaceId"),
                            "directory": directory, "status": "idle",
                            "securityBinding": binding}
            self.data["conversations"][conversation["id"]] = conversation
            self._save()
            return conversation

    def conversation(self, conversation_id):
        with self.lock:
            found = self.data["conversations"].get(conversation_id)
            if found is None and self.flaw == "adopt":
                return {"id": conversation_id, "workspaceId": "ws_adopted", "status": "idle"}
            if found is None:
                raise Failure("Conversation not found", 404, "not_found")
            return found

    def start_run(self, conversation_id, body):
        binding = self._security(body)
        with self.lock:
            conversation = self.data["conversations"].get(conversation_id)
            if conversation is None:
                raise Failure("Conversation not found", 404, "not_found")
            for run in self.data["runs"].values():
                if (run["conversationId"] == conversation_id
                        and run["clientRunId"] == body.get("clientRunId")):
                    return run
            model = body.get("model")
            if model not in (None, MODEL) and self.flaw != "unknown_model":
                raise Failure("Model is unavailable", 400, "unsupported_model")
            if conversation["status"] != "idle" and self.flaw != "busy_start":
                raise Failure("Conversation is busy", 409, "conversation_busy")
            text = body["input"][0]["text"]
            run = {"id": _id("run"), "clientRunId": body.get("clientRunId"),
                   "conversationId": conversation_id, "phase": "active",
                   "activeState": "running", "outcome": None, "result": "", "error": "",
                   "securityBinding": binding}
            self.data["runs"][run["id"]] = run
            conversation["status"] = "busy"
            if "echo wb-conformance" in text and self.flaw == "unasked":
                Path(conversation["directory"], "wb-conformance.txt").write_text("wb-conformance\n")
                threading.Timer(0.2, self._finish, (run["id"], "DONE")).start()
            elif "echo wb-conformance" in text:
                self._ask(run)
            else:
                result = ("model unavailable" if model not in (None, MODEL)
                          and self.flaw != "unknown_model" else "READY")
                threading.Timer(0.2, self._finish, (run["id"], result)).start()
            self._save()
            self._emit("run.started", run)
            return run

    def find_run(self, conversation_id, client_run_id):
        with self.lock:
            for run in self.data["runs"].values():
                if run["conversationId"] == conversation_id and run["clientRunId"] == client_run_id:
                    return run
        raise Failure("Run not found", 404, "not_found")

    def run(self, run_id):
        with self.lock:
            if run_id not in self.data["runs"]:
                raise Failure("Run not found", 404, "not_found")
            return self.data["runs"][run_id]

    def cancel(self, run_id):
        with self.lock:
            run = self.run(run_id)
            if run["phase"] != "terminal":
                run.update(phase="terminal", activeState=None, outcome="cancelled")
                self.data["conversations"][run["conversationId"]]["status"] = "idle"
                for interaction in self.data["interactions"].values():
                    if interaction["runId"] == run_id and interaction["state"] == "pending":
                        interaction["state"] = "stale"
                self._save()
            return run

    def interactions(self, run_id):
        with self.lock:
            self.run(run_id)
            return {"interactions": [item for item in self.data["interactions"].values()
                                     if item["runId"] == run_id and item["state"] == "pending"]}

    def resolve(self, interaction_id, body):
        with self.lock:
            interaction = self.data["interactions"].get(interaction_id)
            if interaction is None:
                raise Failure("Interaction not found", 404, "not_found")
            if interaction["state"] != "pending":
                raise Failure("Interaction is no longer live", 409, "interaction_stale")
            choices = {choice["id"]: choice for choice in interaction["choices"]}
            if body.get("choiceId") not in choices:
                raise Failure("Choice is not available", 400, "invalid_arguments")
            interaction["state"] = "resolved"
            run = self.data["runs"][interaction["runId"]]
            run["activeState"] = "running"
            self._save()
        semantic = choices[body["choiceId"]]["semantic"]
        threading.Timer(0.1, self._finish, (run["id"], semantic)).start()
        return {"ok": True}

    def activities(self, run_id):
        with self.lock:
            self.run(run_id)
            return {"activities": [item for item in self.data["activities"].values()
                                   if item["runId"] == run_id]}

    def activity(self, activity_id):
        with self.lock:
            if activity_id not in self.data["activities"]:
                raise Failure("Activity not found", 404, "not_found")
            return self.data["activities"][activity_id]

    def events(self, after, wait_ms):
        with self.lock:
            if after >= self.cursor and wait_ms:
                self.lock.wait(wait_ms / 1000)
            return {"instanceId": self.instance_id, "cursor": self.cursor,
                    "events": [e for e in self.events_buffer if e["cursor"] > after][:100]}


if __name__ == "__main__":
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    PluginServer(ConformanceAgent).serve(sys.stdin.buffer, out)
