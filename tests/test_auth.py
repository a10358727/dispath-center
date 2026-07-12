"""共享 token 認證中介層（PLAN.md C）：
- AUTH_TOKEN 沒設定 -> 不驗證。
- AUTH_TOKEN 有設定 -> 除了 GET / 與 /static/* 外，都要求 X-Auth-Token 相符。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def auth_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AUTH_TOKEN", "secret-token")

    import app.main as main_module

    with TestClient(main_module.app) as client:
        yield client, main_module


def test_no_token_configured_allows_all_requests(api_client):
    client, _main = api_client
    # AUTH_TOKEN 未設定（api_client fixture 用 monkeypatch.delenv 確保），
    # 不帶任何 header 也能存取一般 API。
    assert client.get("/servers").status_code == 200
    assert client.get("/jobs").status_code == 200


def test_index_exempt_from_auth(auth_client):
    client, _main = auth_client
    resp = client.get("/")
    assert resp.status_code == 200


def test_missing_token_is_401(auth_client):
    client, _main = auth_client
    resp = client.get("/servers")
    assert resp.status_code == 401


def test_wrong_token_is_401(auth_client):
    client, _main = auth_client
    resp = client.get("/servers", headers={"X-Auth-Token": "wrong"})
    assert resp.status_code == 401


def test_correct_token_is_ok(auth_client):
    client, _main = auth_client
    resp = client.get("/servers", headers={"X-Auth-Token": "secret-token"})
    assert resp.status_code == 200


def test_correct_token_allows_dispatch(auth_client):
    client, _main = auth_client
    resp = client.post(
        "/dispatch",
        json={"command": "sleep 60"},
        headers={"X-Auth-Token": "secret-token"},
    )
    assert resp.status_code == 200
