from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    selected_date = datetime.now(UTC).date()
    create_probe(
        connection,
        probe_id="ru-dc-1",
        name="Russia Datacenter",
        region="Russia",
        token_hash="probe-token-hash",
    )
    create_probe(
        connection,
        probe_id="eu-dc-1",
        name="Europe Datacenter",
        region="Europe",
        token_hash="probe-token-hash",
    )
    create_check_result(
        connection,
        site_id=site.id,
        probe_id="ru-dc-1",
        checked_at=datetime.combine(selected_date, datetime.min.time(), tzinfo=UTC)
        + timedelta(hours=10),
        result_status="ok",
        status_group="2xx",
        http_status=200,
        response_time_ms=123,
    )
    create_check_result(
        connection,
        site_id=site.id,
        probe_id="eu-dc-1",
        checked_at=datetime.combine(selected_date, datetime.min.time(), tzinfo=UTC)
        + timedelta(hours=10, minutes=1),
        result_status="server_error",
        status_group="5xx",
        http_status=503,
        response_time_ms=820,
    )
    create_check_result(
        connection,
        site_id=site.id,
        probe_id="eu-dc-1",
        checked_at=datetime.combine(
            selected_date - timedelta(days=1),
            datetime.min.time(),
            tzinfo=UTC,
        )
        + timedelta(hours=10, minutes=1),
        result_status="network_error",
        status_group="network_error",
        error_type="timeout",
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
    assert "Response Time History" in dashboard_response.text
    assert "aria-label=\"Response time by probe for selected date\"" in dashboard_response.text
    assert "probe-ru-dc-1" in dashboard_response.text
    assert "probe-eu-dc-1" in dashboard_response.text
    assert "503" in dashboard_response.text


def test_dashboard_status_badges_use_distinct_5xx_and_network_error_styles(
    dashboard_client,
) -> None:
    dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )
    dashboard_response = dashboard_client.get("/dashboard")

    assert dashboard_response.status_code == 200
    assert (
        ".status-5xx { background: #ffccc7; color: #820014; }"
        in dashboard_response.text
    )
    assert (
        ".status-network_error { background: #5c0011; color: #fff1f0; }"
        in dashboard_response.text
    )
    assert ".status-5xx, .status-network_error" not in dashboard_response.text
    assert '<span class="status status-5xx">5xx</span>' in dashboard_response.text
    assert (
        '<span class="status status-network_error">network_error</span>'
        in dashboard_response.text
    )


def test_dashboard_can_show_selected_date_history(dashboard_client) -> None:
    dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )
    selected_date = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    dashboard_response = dashboard_client.get(f"/dashboard?date={selected_date}")

    assert dashboard_response.status_code == 200
    assert f'value="{selected_date}"' in dashboard_response.text
    assert "network_error" in dashboard_response.text
    assert "timeout" in dashboard_response.text
    assert "No response time data for the selected date." in dashboard_response.text


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
