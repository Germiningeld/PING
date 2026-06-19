from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from central.app.auth import ADMIN_SESSION_COOKIE, hash_admin_password
from central.app.main import app
from central.app.persistence import (
    connect_database,
    create_check_result,
    create_probe,
    create_site,
    initialize_database,
)
from central.app.probe_api import get_database_connection


@pytest.fixture
def dashboard_client(tmp_path, monkeypatch):
    database_path = tmp_path / "central.sqlite3"
    connection = connect_database(database_path)
    initialize_database(connection)
    site = create_site(connection, name="Example", url="https://example.com/")
    create_probe(
        connection,
        probe_id="ru-dc-1",
        name="Russia Datacenter",
        region="Russia",
        token_hash="probe-token-hash",
    )
    create_check_result(
        connection,
        site_id=site.id,
        probe_id="ru-dc-1",
        checked_at=datetime(2026, 6, 19, 10, 0, tzinfo=UTC),
        result_status="ok",
        status_group="2xx",
        http_status=200,
        response_time_ms=123,
    )
    connection.close()

    monkeypatch.setenv("PING_ADMIN_USERNAME", "admin")
    monkeypatch.setenv(
        "PING_ADMIN_PASSWORD_HASH",
        hash_admin_password("correct-password", salt="test-salt"),
    )
    monkeypatch.setenv("PING_ADMIN_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("PING_COOKIE_SECURE", "false")

    def override_database_connection():
        connection = connect_database(database_path)
        initialize_database(connection)
        yield connection
        connection.close()

    app.dependency_overrides[get_database_connection] = override_database_connection
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_database_connection, None)


def test_dashboard_redirects_unauthenticated_user_to_login(dashboard_client) -> None:
    response = dashboard_client.get("/dashboard", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_admin_can_login_and_see_dashboard_shell(dashboard_client) -> None:
    response = dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    assert ADMIN_SESSION_COOKIE in response.cookies

    dashboard_response = dashboard_client.get("/dashboard")

    assert dashboard_response.status_code == 200
    assert "PING Dashboard" in dashboard_response.text
    assert "Example" in dashboard_response.text
    assert "Russia Datacenter" in dashboard_response.text
    assert "2xx" in dashboard_response.text
    assert "200" in dashboard_response.text


def test_admin_can_logout(dashboard_client) -> None:
    login_response = dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )
    assert ADMIN_SESSION_COOKIE in login_response.cookies

    logout_response = dashboard_client.post("/logout", follow_redirects=False)
    dashboard_response = dashboard_client.get("/dashboard", follow_redirects=False)

    assert logout_response.status_code == 303
    assert logout_response.headers["location"] == "/login"
    assert dashboard_response.status_code == 303
    assert dashboard_response.headers["location"] == "/login"


def test_login_rejects_invalid_password(dashboard_client) -> None:
    response = dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "wrong-password"},
    )

    assert response.status_code == 401
    assert "Invalid username or password." in response.text
    assert ADMIN_SESSION_COOKIE not in response.cookies
