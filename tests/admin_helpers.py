"""Authenticate tests through the actual account and first-login endpoints."""
from starlette.testclient import TestClient

TEST_PASSWORD = "test-admin-password"


def admin_cookie(app):
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        response = client.post("/api/login", json={"username": "admin", "password": "admin"})
        if response.status_code == 401:
            response = client.post("/api/login", json={"username": "admin", "password": TEST_PASSWORD})
        assert response.status_code == 200, response.text
        if response.json()["must_change_password"]:
            response = client.post("/api/account/password", json={
                "current_password": "admin", "new_password": TEST_PASSWORD})
            assert response.status_code == 200, response.text
        return "wb-session=" + response.cookies["wb-session"]
