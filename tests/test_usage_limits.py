from admin_helpers import admin_cookie
"""Account usage-limits visibility: native normalization, Node/Bridge path,
Manager API route, and Discord enrichment.

Payloads follow the official current Codex app-server
account/rateLimits/read schema: ``rateLimits`` is ONE required
RateLimitSnapshot object (with nested primary/secondary RateLimitWindow)
and ``rateLimitsByLimitId`` is an optional object of RateLimitSnapshots.
Account quota is separate from run-scoped token usage and from adapter
health/status semantics throughout.
"""
import socket
import threading
import time
from types import MethodType

import pytest
import uvicorn
from starlette.testclient import TestClient

from test_codex_host_adapter import FakeCodexRpc

from workspace_bridge.api import make_admin
from workspace_bridge.node_service import NodeService
from workspace_bridge.notifications import (DiscordChannel, NotificationEvent,
                                            NotificationManager,
                                            codex_quota_summary)
from workspace_bridge.runtime import RuntimeUnsupported
from workspace_bridge.security import BridgeError, digest
from workspace_bridge.wbrp import (Descriptor, HttpRuntimeAdapter,
                                   validate_usage_limits)
from workspace_bridge.codex_host_adapter import (AdapterFailure,
                                                 CodexHostAdapter, make_app)

ADAPTER_ID = "adapter_000000000000000000000001"


@pytest.fixture
def codex_adapter(tmp_path):
    """Mirror the test_codex_host_adapter fixture for this focused file."""
    projects = tmp_path / "projects"
    projects.mkdir()
    workspace = projects / "workspace"
    workspace.mkdir()
    rpc = FakeCodexRpc()
    adapter = CodexHostAdapter(tmp_path / "adapter-state", projects, rpc=rpc)
    yield adapter, workspace, rpc
    adapter.close()


def _normalize_via_adapter(rpc):
    """Build a minimal CodexHostAdapter around an rpc and read its quota."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        projects = Path(tmp) / "projects"
        projects.mkdir()
        native = CodexHostAdapter(Path(tmp) / "state", projects, rpc=rpc)
        try:
            return native.usage_limits()
        finally:
            native.close()


def snapshot(**overrides):
    """One official-schema-shaped RateLimitSnapshot with common metadata."""
    base = {
        "limitId": "codex_5h", "limitName": "Codex 5 hour", "planType": "pro",
        "primary": {"usedPercent": 31, "windowDurationMins": 300,
                    "resetsAt": 1800000000},
        "secondary": {"usedPercent": 39, "windowDurationMins": 10080,
                      "resetsAt": None},
        "rateLimitReachedType": None, "spendControlReached": None,
        "credits": None, "individualLimit": None,
        "accountId": "acc_secret", "rateLimitUpsell": {"cta": "upgrade"},
        "futureField": {"unknown": True},
    }
    base.update(overrides)
    return base


def test_descriptor_advertises_usage_limits(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    assert adapter.descriptor()["features"]["usageLimits"] == 1
    assert ("account/rateLimits/read", {}) not in rpc.calls


def test_usage_limits_normalizes_by_limit_id_snapshots_with_nested_windows():
    rpc = FakeCodexRpc()
    rpc.rate_limits = {
        "rateLimits": snapshot(limitId="fallback-should-not-win"),
        "rateLimitsByLimitId": {
            "codex_7d": snapshot(limitId="codex_7d", limitName="Codex weekly",
                                 primary={"usedPercent": 39,
                                          "windowDurationMins": 10080,
                                          "resetsAt": 1800000000},
                                 secondary=None),
            "codex_5h": snapshot(limitId="codex_5h",
                                 primary={"usedPercent": 100,
                                          "windowDurationMins": 300,
                                          "resetsAt": 1800000000},
                                 secondary={"usedPercent": 250,
                                            "windowDurationMins": 10080,
                                            "resetsAt": None}),
        },
        "ordinaryUsageAllowed": True,
        "accountId": "acc_secret",
        "rateLimitUpsell": {"cta": "upgrade"},
    }
    result = _normalize_via_adapter(rpc)
    assert result["available"] is True
    assert result["ordinaryUsageAllowed"] is True
    assert [bucket["limitId"] for bucket in result["buckets"]] == [
        "codex_5h", "codex_7d"]
    assert result["buckets"][0]["windows"] == [
        {"usedPercent": 100, "remainingPercent": 0,
         "windowDurationMins": 300, "resetsAt": 1800000000},
        {"usedPercent": 250, "remainingPercent": 0,
         "windowDurationMins": 10080, "resetsAt": None},
    ]
    assert result["buckets"][1]["windows"] == [
        {"usedPercent": 39, "remainingPercent": 61,
         "windowDurationMins": 10080, "resetsAt": 1800000000},
    ]
    # accountId and raw upsell payloads never cross the boundary.
    assert "accountId" not in str(result)
    assert "upsell" not in str(result).lower()
    assert "futureField" not in str(result)
    # The single-object rateLimits fallback is ignored when byLimitId wins.
    assert all(bucket["limitId"] != "fallback-should-not-win"
               for bucket in result["buckets"])


def test_usage_limits_falls_back_to_required_single_rate_limits_object(
        codex_adapter):
    adapter, workspace, rpc = codex_adapter
    rpc.rate_limits = {"rateLimits": snapshot(
        limitId="weekly", limitName="Weekly",
        primary={"usedPercent": 39, "windowDurationMins": 10080, "resetsAt": 1},
        secondary={"usedPercent": 250, "windowDurationMins": None,
                   "resetsAt": None})}
    result = adapter.usage_limits()
    assert result["available"] is True
    assert result["ordinaryUsageAllowed"] is None
    assert len(result["buckets"]) == 1
    bucket = result["buckets"][0]
    assert bucket["limitId"] == "weekly" and bucket["planType"] == "pro"
    assert bucket["windows"] == [
        {"usedPercent": 39, "remainingPercent": 61,
         "windowDurationMins": 10080, "resetsAt": 1},
        {"usedPercent": 250, "remainingPercent": 0,
         "windowDurationMins": None, "resetsAt": None},
    ]
    assert "accountId" not in str(result)


def test_usage_limits_primary_only_weekly_and_secondary_only_windows(
        codex_adapter):
    adapter, workspace, rpc = codex_adapter
    rpc.rate_limits = {"rateLimits": snapshot(
        limitId="weekly-only", limitName=None,
        primary={"usedPercent": 5,
                 "windowDurationMins": None,
                 "resetsAt": None},
        secondary=None,
        credits={"hasCredits": True,
                 "unlimited": False,
                 "balance": "10.00"},
        individualLimit={"limit": "$10", "used": "$3",
                         "remainingPercent": 70,
                         "resetsAt": 55})}
    result = adapter.usage_limits()
    assert len(result["buckets"]) == 1
    fallback = result["buckets"][0]
    assert fallback["limitId"] == "weekly-only"
    assert fallback["windows"] == [{"usedPercent": 5, "remainingPercent": 95,
                                    "windowDurationMins": None,
                                    "resetsAt": None}]
    assert fallback["credits"] == {"hasCredits": True, "unlimited": False,
                                   "balance": "10.00"}
    assert fallback["individualLimit"] == {"limit": "$10", "used": "$3",
                                           "remainingPercent": 70,
                                           "resetsAt": 55}
    # Secondary-only window: schema permits primary=null.
    rpc.rate_limits = {"rateLimitsByLimitId": {
        "codex_secondary_only": snapshot(
            limitId="codex_secondary_only",
            primary=None,
            secondary={"usedPercent": 50, "windowDurationMins": 7,
                       "resetsAt": None},
            spendControlReached=True,
            rateLimitReachedType="weekly_cap")}}
    result = adapter.usage_limits()
    assert len(result["buckets"]) == 1
    secondary_only = result["buckets"][0]
    assert secondary_only["spendControlReached"] is True
    assert secondary_only["rateLimitReachedType"] == "weekly_cap"
    assert secondary_only["windows"] == [
        {"usedPercent": 50, "remainingPercent": 50,
         "windowDurationMins": 7, "resetsAt": None}]
    # Unknown window durations stay numeric without a guessed label.


def test_usage_limits_malformed_windows_and_metadata_only_bucket(
        codex_adapter):
    adapter, workspace, rpc = codex_adapter
    rpc.rate_limits = {"rateLimits": snapshot(
        limitId="codex_meta",
        primary={"usedPercent": "bogus", "windowDurationMins": 300},
        secondary={"usedPercent": None, "windowDurationMins": 10080},
        credits={"hasCredits": True, "unlimited": False, "balance": "4.00"},
        spendControlReached=True)}
    result = adapter.usage_limits()
    # No valid windows, but credits/spend-control metadata keeps the bucket
    # observable; remaining-quota surfaces as unavailable (never 0%).
    assert len(result["buckets"]) == 1
    bucket = result["buckets"][0]
    assert bucket["windows"] == []
    assert bucket["credits"] == {"hasCredits": True, "unlimited": False,
                                 "balance": "4.00"}
    assert bucket["spendControlReached"] is True


def test_usage_limits_unavailable_when_rpc_has_no_data(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    rpc.rate_limits = {}
    with pytest.raises(AdapterFailure) as exc:
        adapter.usage_limits()
    assert exc.value.code == "runtime_unavailable"
    rpc.rate_limits = snapshot(primary={"usedPercent": None})
    with pytest.raises(AdapterFailure) as empty:
        adapter.usage_limits()
    assert empty.value.code == "runtime_unavailable"
    rpc.rate_limits = snapshot()
    rpc.rate_limits_error = "denied"
    with pytest.raises(AdapterFailure) as failure:
        adapter.usage_limits()
    assert failure.value.code == "runtime_unavailable"
    assert "denied" not in str(failure.value)


def test_usage_limits_http_route_requires_token(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    rpc = FakeCodexRpc()
    rpc.rate_limits = {"rateLimits": snapshot(
        limitId="codex", limitName=None, planType=None,
        primary={"usedPercent": 40, "windowDurationMins": 300,
                 "resetsAt": None},
        secondary=None)}
    native = CodexHostAdapter(tmp_path / "state", projects, rpc=rpc)
    client = TestClient(make_app(native, "secret-token"))
    response = client.get("/v1/usage-limits", headers={"X-Runtime-Token": "wrong"})
    assert response.status_code == 401
    response = client.get("/v1/usage-limits", headers={"X-Runtime-Token": "secret-token"})
    assert response.status_code == 200
    body = response.json()
    assert body == {"available": True, "ordinaryUsageAllowed": None,
                    "buckets": [{"limitId": "codex", "limitName": None,
                                 "planType": None,
                                 "rateLimitReachedType": None,
                                 "spendControlReached": None,
                                 "credits": None, "individualLimit": None,
                                 "windows": [{"usedPercent": 40,
                                              "remainingPercent": 60,
                                              "windowDurationMins": 300,
                                              "resetsAt": None}]}]}
    native.close()


def test_validate_usage_limits_fails_closed_on_invalid_shapes():
    with pytest.raises(BridgeError):
        validate_usage_limits("nope")
    with pytest.raises(BridgeError):
        validate_usage_limits({"available": "yes", "buckets": []})
    with pytest.raises(BridgeError):
        validate_usage_limits({"available": True, "buckets": "no"})
    with pytest.raises(BridgeError):
        validate_usage_limits({"available": True, "ordinaryUsageAllowed": 3,
                               "buckets": []})
    assert validate_usage_limits({"available": False, "buckets": []}) == {
        "available": False, "ordinaryUsageAllowed": None, "buckets": []}


def test_validate_usage_limits_drops_malformed_buckets_and_derives_remaining():
    result = validate_usage_limits({"available": True,
                                    "ordinaryUsageAllowed": False,
                                    "buckets": [
                                        {"limitId": "codex",
                                         "windows": [
                                             {"usedPercent": 31,
                                              "windowDurationMins": 300}],
                                         "unexpected": {"deep": True}},
                                        {"limitId": "nowindow",
                                         "windows": [{"usedPercent": None}]},
                                        {"limitId": "overused",
                                         "windows": [
                                             {"usedPercent": 200,
                                              "windowDurationMins": -5,
                                              "resetsAt": 2 ** 70}]},
                                        {"limitId": "no-windows",
                                         "windows": "bogus"},
                                    ]})
    assert result["ordinaryUsageAllowed"] is False
    assert len(result["buckets"]) == 2
    first, second = result["buckets"]
    assert first["limitId"] == "codex"
    assert first["windows"] == [{"usedPercent": 31, "remainingPercent": 69,
                                 "windowDurationMins": 300, "resetsAt": None}]
    assert "unexpected" not in str(result)
    assert second["windows"] == [{"usedPercent": 200, "remainingPercent": 0,
                                  "windowDurationMins": None,
                                  "resetsAt": None}]
    # A bucket with neither valid windows nor metadata is dropped.
    assert all(bucket["limitId"] != "no-windows" for bucket in result["buckets"])


def test_http_usage_limits_requires_advertised_capability():
    """Unsupported adapters fail as unsupported, never as unhealthy."""
    descriptor = Descriptor("pi", "Pi", "1.0", "0", "instance", {
        "models": 1, "conversations": 1, "runs": 1,
        "activities": 1, "interactions": 1})
    client = HttpRuntimeAdapter(ADAPTER_ID, "pi", "http://127.0.0.1:1", "token")
    client.descriptor = MethodType(lambda self: descriptor, client)
    with pytest.raises(RuntimeUnsupported):
        client.usage_limits()


def start_codex_adapter_server(tmp_path):
    base = tmp_path / "quota-native"
    projects = base / "projects"
    projects.mkdir(parents=True)
    rpc = FakeCodexRpc()
    rpc.rate_limits = {"rateLimits": snapshot(
        limitId="codex", limitName="Codex",
        primary={"usedPercent": 31, "windowDurationMins": 300,
                 "resetsAt": 1800000000},
        secondary={"usedPercent": 39, "windowDurationMins": 10080,
                   "resetsAt": 1800000000}),
        "ordinaryUsageAllowed": True}
    native = CodexHostAdapter(base / "adapter-state", projects, rpc=rpc)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(make_app(native, "secret-token"),
                                           host="127.0.0.1", port=port,
                                           log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.01)
    assert server.started
    return native, server, thread, port


def test_node_runtime_call_usage_limits_without_workspace(tmp_path):
    native, server, thread, port = start_codex_adapter_server(tmp_path)
    try:
        config = {"allowed_roots": [str(tmp_path)],
                  "node_token_hash": digest(b"node-token"), "host": "127.0.0.1"}
        node = NodeService(tmp_path / "node-state", config)
        try:
            created = node.save_adapter({
                "name": "Quota adapter", "runtime_type": "codex",
                "base_url": f"http://127.0.0.1:{port}", "token": "secret-token"})
            result = node.runtime_call(created["id"], "usage_limits", {
                "arguments": {},
                "expected_adapter_revision": created["revision"]})
            assert result["available"] is True
            assert result["ordinaryUsageAllowed"] is True
            assert len(result["buckets"]) == 1
            bucket = result["buckets"][0]
            assert bucket["limitId"] == "codex"
            assert bucket["windows"] == [
                {"usedPercent": 31, "remainingPercent": 69,
                 "windowDurationMins": 300, "resetsAt": 1800000000},
                {"usedPercent": 39, "remainingPercent": 61,
                 "windowDurationMins": 10080, "resetsAt": 1800000000}]
            assert "accountId" not in str(result)
        finally:
            node.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        native.close()


def test_service_usage_limits_and_admin_api_route(env, tmp_path):
    native, server, thread, port = start_codex_adapter_server(tmp_path)
    try:
        service = env["service"]
        created = service.adapter_registry.create({
            "name": "Codex quota", "runtime_type": "codex",
            "base_url": f"http://127.0.0.1:{port}", "token": "secret-token",
            "enabled": True, "node_id": env["node_id"]})
        adapter_id = created["id"]
        summary = service.run_coordinator.usage_limits(adapter_id)
        assert summary["buckets"][0]["windows"][0]["remainingPercent"] == 69
        import asyncio
        import httpx

        async def fetch():
            app = make_admin(service)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://127.0.0.1:8766") as client:
                token = admin_cookie(app)
                headers = {"Cookie": token}
                unauth = await client.get(f"/api/adapters/{adapter_id}/usage-limits")
                ok = await client.get(f"/api/adapters/{adapter_id}/usage-limits",
                                      headers=headers)
                return unauth, ok
        unauth, ok = asyncio.run(fetch())
        assert unauth.status_code == 401
        assert ok.status_code == 200
        body = ok.json()
        assert body["available"] is True
        assert [bucket["limitId"] for bucket in body["buckets"]] == ["codex"]
        assert len(body["buckets"][0]["windows"]) == 2
        assert "accountId" not in str(body)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        native.close()


def test_admin_usage_limits_unsupported_and_disabled(env):
    service = env["service"]
    import asyncio
    import httpx

    async def fetch(adapter_id):
        app = make_admin(service)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://127.0.0.1:8766") as client:
            token = admin_cookie(app)
            return await client.get(f"/api/adapters/{adapter_id}/usage-limits",
                                    headers={"Cookie": token})
    # Disabled adapter fails cleanly, not as an unhealthy probe.
    disabled = service.adapter_registry.create({
        "name": "Disabled quota", "runtime_type": "codex",
        "base_url": "http://127.0.0.1:9", "token": "t", "enabled": False})
    response = asyncio.run(fetch(disabled["id"]))
    assert response.status_code == 400
    assert response.json()["code"] == "adapter_disabled"
    # Unknown adapter.
    assert asyncio.run(fetch("adapter_" + "f" * 24)).status_code == 400


def test_quota_summary_selects_one_bucket_and_at_most_two_windows():
    limits = {"available": True, "ordinaryUsageAllowed": True, "buckets": [
        {"limitId": "other", "windows": [
            {"usedPercent": 40, "remainingPercent": 60,
             "windowDurationMins": 1440},
            {"usedPercent": 45, "remainingPercent": 55,
             "windowDurationMins": 60},
        ]},
        {"limitId": "codex", "windows": [
            {"usedPercent": 31, "remainingPercent": 69,
             "windowDurationMins": 300},
            {"usedPercent": 39, "remainingPercent": 61,
             "windowDurationMins": 10080},
        ]},
    ]}
    assert codex_quota_summary(limits) == "5h 69% left · 7d 61% left"
    # At most two windows even when the bucket carries more.
    many = {"buckets": [{"limitId": "codex", "windows": [
        {"remainingPercent": 69, "windowDurationMins": 300},
        {"remainingPercent": 61, "windowDurationMins": 10080},
        {"remainingPercent": 50, "windowDurationMins": 1440},
        {"remainingPercent": 40, "windowDurationMins": 60},
    ]}]}
    assert codex_quota_summary(many) == "5h 69% left · 7d 61% left"
    # Fallback to the first usable bucket when no canonical codex bucket.
    fallback = {"buckets": [
        {"limitId": "a", "windows": [
            {"remainingPercent": 80, "windowDurationMins": 90},
            {"remainingPercent": 50},
        ]},
        {"limitId": "b", "windows": [
            {"remainingPercent": 10, "windowDurationMins": 10080}]},
    ]}
    assert codex_quota_summary(fallback) == "90m 80% left · 50% left"
    # Without an explicit ordinaryUsageAllowed=false and without windows,
    # the field is omitted; no percentage is invented.
    assert codex_quota_summary({"buckets": []}) == ""
    assert codex_quota_summary(None) == ""
    # Buckets without valid windows contribute nothing.
    assert codex_quota_summary({"buckets": [
        {"limitId": "codex", "windows": []},
        {"limitId": "other", "windows": [{"usedPercent": "bogus"}]},
    ]}) == ""
    unavailable = {"available": True, "ordinaryUsageAllowed": False,
                   "buckets": [{"limitId": "codex", "windows": [
                       {"remainingPercent": 61,
                        "windowDurationMins": 10080}]}]}
    assert codex_quota_summary(unavailable) == (
        "Ordinary usage unavailable · 7d 61% left")


def test_quota_summary_bounded():
    assert codex_quota_summary({"buckets": [
        {"limitId": "codex", "windows": [{"remainingPercent": 50}]}]}) == (
        "50% left")
    assert len(codex_quota_summary({"buckets": [
        {"limitId": "codex", "windows": [
            {"remainingPercent": 50, "windowDurationMins": minutes}
            for minutes in range(1, 60)]}]})) <= 160


def test_quota_summary_surfaces_ordinary_unavailable_without_windows():
    # ordinaryUsageAllowed=false must never be lost when windows are absent,
    # and no percentage is ever invented.
    assert codex_quota_summary({"available": True,
                                "ordinaryUsageAllowed": False,
                                "buckets": [{"limitId": "codex",
                                             "windows": []}]}) == (
        "Ordinary usage unavailable")
    assert codex_quota_summary({"available": True,
                                "ordinaryUsageAllowed": False,
                                "buckets": []}) == "Ordinary usage unavailable"
    assert codex_quota_summary({"ordinaryUsageAllowed": False}) == (
        "Ordinary usage unavailable")
    # Without an explicit false and without windows, the field is omitted.
    assert codex_quota_summary({"available": True,
                                "ordinaryUsageAllowed": True,
                                "buckets": []}) == ""
    for value in (codex_quota_summary({"available": True,
                                       "ordinaryUsageAllowed": False,
                                       "buckets": [{"windows": []}]}),):
        assert "%" not in value


def test_discord_payload_includes_quota_field():
    event = NotificationEvent.build(
        event_type="run_completed", run_id="run_quota", workspace_id="ws_alpha",
        workspace_name="Alpha", handoff_title="Improve add",
        adapter_id=ADAPTER_ID, adapter_name="Local Codex", runtime_type="codex",
        occurred_at="2026-09-19T00:00:00+00:00")
    payload = DiscordChannel._payload(event)
    assert all(field["name"] != "Quota" for field in payload["embeds"][0]["fields"])
    from dataclasses import replace
    # Concise window summary without a redundant "Quota —" prefix.
    enriched = replace(event, quota="5h 69% left · 7d 61% left")
    fields = DiscordChannel._payload(enriched)["embeds"][0]["fields"]
    quota = next(field for field in fields if field["name"] == "Quota")
    assert quota["value"] == "5h 69% left · 7d 61% left"
    # Explicit ordinary-unavailable state, with and without windows.
    fields = DiscordChannel._payload(replace(
        event, quota="Ordinary usage unavailable"))["embeds"][0]["fields"]
    assert next(field for field in fields
                if field["name"] == "Quota")["value"] == (
        "Ordinary usage unavailable")
    fields = DiscordChannel._payload(replace(
        event, quota="Ordinary usage unavailable · 5h 69% left"))[
        "embeds"][0]["fields"]
    assert next(field for field in fields
                if field["name"] == "Quota")["value"] == (
        "Ordinary usage unavailable · 5h 69% left")


def test_drain_enriches_codex_delivery_and_omits_on_failure(env):
    from notification_fakes import RecordingChannel
    service = env["service"]
    original = service.notification_manager
    original.close()

    def lookup(adapter_id):
        return "5h 69% left · 7d 61% left"

    channel = RecordingChannel()
    manager = NotificationManager(service, [channel], quota_lookup=lookup)
    service.notification_manager = manager
    manager.start()
    try:
        manager.publish(NotificationEvent.build(
            event_type="run_completed", run_id="run_codex",
            workspace_id="ws_alpha", workspace_name="Alpha",
            handoff_title="Improve add", adapter_id="adapter_000000000000000000000001",
            adapter_name="Local Codex", runtime_type="codex",
            occurred_at="2026-09-19T00:00:00+00:00"))
        manager.publish(NotificationEvent.build(
            event_type="run_completed", run_id="run_pi",
            workspace_id="ws_alpha", workspace_name="Alpha",
            handoff_title="Improve add", adapter_id="adapter_000000000000000000000001",
            adapter_name="Local Pi", runtime_type="pi",
            occurred_at="2026-09-19T00:00:01+00:00"))
        assert manager._idle.wait(2), "drain did not complete"
        delivered = [event for event in channel.calls
                     if event.event_type == "run_completed"]
        assert len(delivered) == 2
        by_runtime = {event.runtime_type: event for event in delivered}
        assert by_runtime["codex"].quota == "5h 69% left · 7d 61% left"
        assert by_runtime["pi"].quota == ""
        # Quota enrichment stays ephemeral: nothing persisted in the outbox.
        row = service.db.execute(
            "SELECT * FROM notification_events WHERE run_id='run_codex'").fetchone()
        assert "quota" not in row.keys()
    finally:
        manager.close()
        service.notification_manager = original


def test_quota_lookup_failure_omits_quota_and_still_delivers(env):
    from notification_fakes import RecordingChannel
    service = env["service"]
    original = service.notification_manager
    original.close()

    def failing_lookup(adapter_id):
        raise BridgeError("quota down", "runtime_unavailable")

    channel = RecordingChannel()
    manager = NotificationManager(service, [channel],
                                  quota_lookup=failing_lookup)
    service.notification_manager = manager
    manager.start()
    try:
        manager.publish(NotificationEvent.build(
            event_type="run_completed", run_id="run_fail",
            workspace_id="ws_alpha", workspace_name="Alpha",
            handoff_title="Improve add", adapter_id="adapter_000000000000000000000002",
            adapter_name="Local Codex", runtime_type="codex",
            occurred_at="2026-09-19T00:00:00+00:00"))
        assert manager._idle.wait(2), "drain did not complete"
        assert len(channel.calls) == 1
        delivered = channel.calls[0]
        assert delivered.quota == ""
        assert delivered.runtime_type == "codex"
        row = service.db.execute(
            "SELECT status FROM notification_deliveries d "
            "JOIN notification_events e ON e.id=d.event_id "
            "WHERE e.run_id='run_fail'").fetchone()
        assert row["status"] == "sent"
    finally:
        manager.close()
        service.notification_manager = original


def test_no_db_schema_for_quota_and_manager_has_no_outbox_quota_column(env):
    service = env["service"]
    tables = {row["name"] for row in service.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert not any("quota" in name for name in tables)
    columns = {row["name"] for row in service.db.execute(
        "SELECT name FROM pragma_table_info('notification_events')")}
    assert "quota" not in columns
