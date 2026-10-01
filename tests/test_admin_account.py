import json
import os

import httpx
import pytest

from workspace_bridge.admin_account import AdminAccount
from workspace_bridge.api import make_admin
from workspace_bridge.cli import initialize, main
from workspace_bridge.security import BridgeError, digest


def test_bootstrap_stores_salted_hash_only(tmp_path):
    state = tmp_path / "state"
    config = initialize(state, 8765, 8766)
    account = AdminAccount(state).read()
    assert "admin_token_hash" not in config
    assert not (state / "admin-token").exists()
    assert account["must_change_password"]
    assert AdminAccount.verify(account, "admin")
    assert not AdminAccount.verify(account, "wrong")
    assert "password" not in account
    assert (state / "admin-account.json").stat().st_mode & 0o777 == 0o600
    assert AdminAccount(state).ensure() == account


async def test_password_changes_and_cli_reset_revoke_sessions(env, capsys):
    app = make_admin(env["service"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8766") as a, httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8766") as b:
        credentials = {"username": "admin", "password": "admin"}
        assert (await a.post("/api/login", json=credentials)).status_code == 200
        assert (await b.post("/api/login", json=credentials)).status_code == 200
        for new in ("short", "admin"):
            assert (await a.post("/api/account/password", json={"current_password": "admin", "new_password": new})).status_code == 400
        assert (await a.post("/api/account/password", json={"current_password": "wrong", "new_password": "long-password"})).status_code == 400
        assert (await a.post("/api/account/password", json={"current_password": "admin", "new_password": "long-password"})).status_code == 200
        assert (await b.get("/api/account")).status_code == 401
        assert (await b.post("/api/login", json=credentials)).status_code == 401
        credentials["password"] = "long-password"
        assert (await b.post("/api/login", json=credentials)).status_code == 200
        assert (await a.post("/api/account/password", json={"current_password": "long-password", "new_password": "second-password"})).status_code == 200
        assert (await b.get("/api/status")).status_code == 401
        assert (await a.get("/api/status")).status_code == 200
        main(["--state", str(env["state"]), "reset-admin-password"])
        assert "password reset" in capsys.readouterr().out
        assert (await a.get("/api/status")).status_code == 401
        credentials["password"] = "admin"
        assert (await b.post("/api/login", json=credentials)).json()["must_change_password"]
        assert (await b.post("/api/bridge", json={"operation": "disable"})).status_code == 403
        # Reload the application: account and the mandatory gate persist.
        reopened = make_admin(env["service"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=reopened), base_url="http://127.0.0.1:8766") as c:
            assert (await c.post("/api/login", json=credentials)).json()["must_change_password"]


async def test_login_throttle_and_invalid_requests(env):
    app = make_admin(env["service"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8766") as c:
        assert (await c.post("/api/login", json={"password": []})).status_code == 401
        assert (await c.post("/api/login", content='x' * 3000, headers={"Content-Type": "application/json"})).status_code == 401
        for _ in range(5):
            assert (await c.post("/api/login", json={"username": "admin", "password": "wrong"})).status_code == 401
        r = await c.post("/api/login", json={"username": "admin", "password": "admin"})
        assert r.status_code == 429 and r.headers["retry-after"] == "60"


async def test_legacy_state_bootstraps_account_and_rejects_old_token(env):
    path = env["state"] / "admin-account.json"
    path.unlink()
    env["service"].config["admin_token_hash"] = digest(b"legacy-secret")
    (env["state"] / "admin-token").write_text("legacy-secret")
    app = make_admin(env["service"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8766") as c:
        assert (await c.post("/api/login", headers={"Authorization": "Bearer legacy-secret"})).status_code == 401
        assert (await c.get("/api/status", headers={"Authorization": "Bearer legacy-secret"})).status_code == 401
        r = await c.post("/api/login", json={"username": "admin", "password": "admin"})
        assert r.json()["must_change_password"]
    assert (env["state"] / "admin-token").read_text() == "legacy-secret"


def test_account_symlinks_and_public_files_rejected(tmp_path):
    state = tmp_path / "state"
    initialize(state, 8765, 8766)
    path = state / "admin-account.json"
    os.chmod(path, 0o644)
    with pytest.raises(BridgeError):
        AdminAccount(state).ensure()
    os.chmod(path, 0o600)
    original = state / "original.json"
    path.rename(original)
    path.symlink_to(original)
    with pytest.raises(OSError):
        AdminAccount(state).reset()
    assert AdminAccount.verify(json.loads(original.read_text()), "admin")


def test_cli_recovery_repairs_corrupt_account_without_changing_config(env):
    path = env['state'] / 'admin-account.json'
    config_before = (env['state'] / 'config.json').read_bytes()
    path.write_text('{broken-json')
    main(['--state', str(env['state']), 'reset-admin-password'])
    account = AdminAccount(env['state']).read()
    assert AdminAccount.verify(account, 'admin') and account['must_change_password']
    assert (env['state'] / 'config.json').read_bytes() == config_before
