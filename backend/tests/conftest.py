import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("SSO_SECRET", "sso-test")
    monkeypatch.setenv("CHANNEL_SECRET", "channel-test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    from app.core import create_app

    return create_app(os.environ["DATABASE_URL"])


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def auth(client, username, password="builder123", space_key="support"):
    passwords = {
        "admin": "admin123",
        "builder": "builder123",
        "operator": "operator123",
        "viewer": "viewer123",
        "finance": "finance123",
    }
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": passwords.get(username, password)},
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    me = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    space = next(item for item in me.json()["spaces"] if item["key"] == space_key)
    return {"Authorization": f"Bearer {token}", "X-Space-Id": str(space["id"])}, space
