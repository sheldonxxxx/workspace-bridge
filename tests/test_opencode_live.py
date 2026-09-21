"""Opt-in live smoke test against a real host OpenCode runtime.

Skipped unless WB_LIVE_OPENCODE=1 and WB_OPENCODE_RUNTIME_URL are supplied. This is
never required for CI and never touches provider credentials directly.
"""
import os

import pytest

from workspace_bridge.runtime import runtime_from_environment


pytestmark = pytest.mark.skipif(
    os.environ.get("WB_LIVE_OPENCODE") != "1" or not os.environ.get("WB_OPENCODE_RUNTIME_URL"),
    reason="live OpenCode smoke is opt-in; set WB_LIVE_OPENCODE=1 and WB_OPENCODE_RUNTIME_URL")


def test_live_runtime_health_and_models():
    runtime = runtime_from_environment()
    assert runtime is not None
    health = runtime.health()
    assert health["adapter_version"]
    directory = os.environ.get("WB_LIVE_OPENCODE_DIRECTORY")
    if directory:
        models = runtime.list_models(directory)
        assert all(m.selector and "/" in m.selector for m in models)


def test_live_v2_permission_snapshot_and_event_path():
    """Opt-in live diagnostic for the V2 permission generation (adapter 0.1.8).

    Safe by construction: it creates a session inside a repository-local
    directory, asks a harmless read-only question, and only *observes* the
    V2 pending snapshot and event stream. It never changes permission
    policy, never replies to a permission, and never touches paths outside
    the given directory. A real permission ask is policy-dependent and may
    not occur; that outcome is reported, not failed.
    """
    import time

    runtime = runtime_from_environment()
    assert runtime is not None
    health = runtime.health()
    assert health["adapter_version"], "adapter version must be reported"
    directory = os.environ.get("WB_LIVE_OPENCODE_DIRECTORY", "")
    assert directory, "set WB_LIVE_OPENCODE_DIRECTORY to a repository-local directory"
    session = runtime.create_session(directory, "Bridge live V2 diagnostic")
    assert session.id
    try:
        pending = runtime.list_pending_permissions(directory, session.id)
        assert pending == [], f"fresh session must have no pending permissions: {pending!r}"
        source = getattr(runtime, "last_permission_source", None)
        print(f"live V2 snapshot: source={source} matched=0 session={session.id}")
        assert source == "v2", f"expected the V2 primary snapshot live, got {source!r}"
        runtime.prompt_async(
            directory, session.id,
            "List the top-level files in this project directory and reply with just "
            "their names. Do not create, modify, or delete anything.",
            None)
        deadline = time.time() + 90
        observed = []
        cursor = 0
        while time.time() < deadline:
            events, cursor = runtime.poll_events(cursor, timeout=10)
            for event in events:
                if event.get("type") == "permission.asked":
                    observed.append(event)
            pending = runtime.list_pending_permissions(directory, session.id)
            if pending or observed:
                break
        print(f"live V2 observe: asks={len(observed)} pending={len(pending)} "
              f"source={getattr(runtime, 'last_permission_source', None)}")
        for item in pending:
            assert item.session_id == session.id
            assert item.generation in ("v1", "v2")
        for event in observed:
            permission = event.get("permission") or {}
            assert permission.get("session_id") == session.id
            assert permission.get("generation") in ("v1", "v2")
    finally:
        try:
            runtime.abort_session(directory, session.id)
        except Exception:
            pass


def test_live_question_snapshot_reports_compatible_source():
    """Opt-in live diagnostic for question polling (adapter 0.1.10).

    Safe by construction: it creates a fresh session inside a
    repository-local directory and only *observes* the pending-question
    snapshot. It never forces the model to ask a question, never replies
    to or rejects anything, and aborts the session afterwards. A fresh
    session must list successfully (V2 primary or V1 compatibility
    fallback) with an empty result and a reported source of v1 or v2;
    runtime_unavailable here means the fallback did not engage and must
    be investigated, not ignored.
    """
    runtime = runtime_from_environment()
    assert runtime is not None
    health = runtime.health()
    assert health["adapter_version"], "adapter version must be reported"
    directory = os.environ.get("WB_LIVE_OPENCODE_DIRECTORY", "")
    assert directory, "set WB_LIVE_OPENCODE_DIRECTORY to a repository-local directory"
    session = runtime.create_session(directory, "Bridge live question diagnostic")
    assert session.id
    try:
        pending = runtime.list_pending_questions(directory, session.id)
        assert pending == [], f"fresh session must have no pending questions: {pending!r}"
        source = getattr(runtime, "last_question_source", None)
        print(f"live question snapshot: source={source} matched=0 session={session.id}")
        assert source in ("v1", "v2"), f"expected a reported v1/v2 source live, got {source!r}"
    finally:
        try:
            runtime.abort_session(directory, session.id)
        except Exception:
            pass
