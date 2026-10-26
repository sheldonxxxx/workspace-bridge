"""A minimal agent plugin for protocol tests: one conversation, scripted runs."""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from workspace_bridge.plugin_host import PluginServer  # noqa: E402


class Failure(Exception):
    def __init__(self, message: str, status: int, code: str):
        super().__init__(message)
        self.status, self.code = status, code


class FakeAgent:
    def __init__(self, params: dict):
        self.settings = params.get("settings") or {}
        self.state = Path(params["state"])
        self.root = params["projectsRoot"]
        self.instance_id = f"fake_instance_{os.getpid()}"
        self.cursor = 0
        self.events_buffer: list[dict] = []
        self.condition = threading.Condition()
        self.runs: dict[str, dict] = {}
        (self.state / "started").write_text(str(os.getpid()))
        # Stray output must never reach the protocol stream.
        print("noise on stdout")
        os.write(1, b"raw noise on fd 1\n")

    def descriptor(self):
        if self.settings.get("mode") == "native-down":
            raise Failure("Native runtime is unavailable", 503, "unavailable")
        return {"protocol": {"major": 1, "minor": 0},
                "runtime": {"id": "pi", "displayName": "Fake", "adapterVersion": "1",
                            "nativeVersion": "1", "instanceId": self.instance_id},
                "features": {"models": 1, "conversations": 1, "runs": 1, "activities": 1,
                             "interactions": 1, "events": 1}}

    def models(self):
        return {"models": [{"selector": "fake", "displayName": "Fake", "default": True}]}

    def create_conversation(self, body):
        if body.get("directory", "").startswith(self.root) is False:
            raise Failure("outside", 400, "root_denied")
        return {"id": "conv1", "workspaceId": body.get("workspaceId"), "status": "idle"}

    def start_run(self, conversation_id, body):
        if self.settings.get("mode") == "busy":
            raise Failure("busy", 409, "conversation_busy")
        run = {"id": "native1", "clientRunId": body.get("clientRunId"),
               "conversationId": conversation_id, "phase": "active",
               "activeState": "running", "outcome": None, "result": "", "error": "",
               "big": "x" * int(self.settings.get("big", "0") or 0)}
        self.runs[run["id"]] = run
        self._emit("run.started", conversation_id, run["id"])
        return run

    def run(self, run_id):
        if run_id not in self.runs:
            raise Failure("missing", 404, "not_found")
        return self.runs[run_id]

    def cancel(self, run_id):
        if self.settings.get("mode") == "crash":
            os._exit(3)
        if self.settings.get("mode") == "slow":
            time.sleep(5)
        return {"ok": True}

    def usage_limits(self):
        raise RuntimeError("secret detail that must not leak")

    def _emit(self, kind, conversation, run=""):
        with self.condition:
            self.cursor += 1
            self.events_buffer.append({"instanceId": self.instance_id, "cursor": self.cursor,
                                       "type": kind, "conversationId": conversation,
                                       "runId": run})
            self.condition.notify_all()

    def events(self, after, wait_ms):
        with self.condition:
            if after >= self.cursor and wait_ms:
                self.condition.wait(wait_ms / 1000)
            return {"instanceId": self.instance_id, "cursor": self.cursor,
                    "events": [e for e in self.events_buffer if e["cursor"] > after][:100]}

    def close(self):
        (self.state / "closed").write_text("1")


if __name__ == "__main__":
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    PluginServer(FakeAgent).serve(sys.stdin.buffer, out)
