from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from central.app.auth import hash_probe_token
from central.app.main import app
from central.app.persistence import (
    connect_database,
    create_probe,
    create_site,
    initialize_database,
    list_check_results_for_site,
)
from central.app.probe_api import get_database_connection


def test_authenticated_probe_can_get_active_config(tmp_path) -> None:
    database_path = _build_database(tmp_path)
    connection = connect_database(database_path)
    site = create_site(connection, name="Example", url="https://example.com/")
    create_site(connection, name="Disabled", url="https://disabled.example/", enabled=False)
    create_probe(
        connection,
        probe_id="ru-dc-1",
        name="Russia Datacenter",
        region="Russia",
        token_hash=hash_probe_token("probe-secret"),
    )
    connection.close()
    client = _build_client(database_path)

    response = client.get(
        "/api/probe/config",
        headers=_auth_headers("ru-dc-1", "probe-secret"),
    )

    assert response.status_code == 200
    assert response.json() == {
        "probe_id": "ru-dc-1",
        "sites": [{"id": site.id, "name": "Example", "url": "https://example.com/"}],
        "check_interval_seconds": 60,
        "timeout_seconds": 10,
        "max_redirects": 0,
    }


def test_probe_api_rejects_missing_token(tmp_path) -> None:
    database_path = _build_database(tmp_path)
    connection = connect_database(database_path)
    create_probe(
        connection,
        probe_id="ru-dc-1",
        name="Russia Datacenter",
        region="Russia",
        token_hash=hash_probe_token("probe-secret"),
    )
    connection.close()
    client = _build_client(database_path)

    response = client.get("/api/probe/config", headers={"X-Probe-Id": "ru-dc-1"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Missing probe bearer token"


def test_probe_api_rejects_unknown_probe(tmp_path) -> None:
    database_path = _build_database(tmp_path)
    client = _build_client(database_path)

    response = client.get(
        "/api/probe/config",
        headers=_auth_headers("missing-probe", "probe-secret"),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown probe"


def test_probe_api_rejects_invalid_token_for_probe_id(tmp_path) -> None:
    database_path = _build_database(tmp_path)
    connection = connect_database(database_path)
    create_probe(
        connection,
        probe_id="ru-dc-1",
        name="Russia Datacenter",
        region="Russia",
        token_hash=hash_probe_token("probe-secret"),
    )
    connection.close()
    client = _build_client(database_path)

    response = client.post(
        "/api/probe/results",
        headers=_auth_headers("ru-dc-1", "wrong-token"),
        json={"results": []},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid probe token"


def test_authenticated_probe_can_submit_batch_results(tmp_path) -> None:
    database_path = _build_database(tmp_path)
    connection = connect_database(database_path)
    site = create_site(connection, name="Example", url="https://example.com/")
    create_probe(
        connection,
        probe_id="eu-dc-1",
        name="Europe Datacenter",
        region="Europe",
        token_hash=hash_probe_token("probe-secret"),
    )
    connection.close()
    client = _build_client(database_path)

    response = client.post(
        "/api/probe/results",
        headers=_auth_headers("eu-dc-1", "probe-secret"),
        json={
            "results": [
                {
                    "site_id": site.id,
                    "checked_at": "2026-06-19T09:30:00+00:00",
                    "result_status": "ok",
                    "status_group": "2xx",
                    "http_status": 200,
                    "response_time_ms": 123,
                },
                {
                    "site_id": site.id,
                    "checked_at": "2026-06-19T09:31:00+00:00",
                    "result_status": "network_error",
                    "status_group": "network_error",
                    "error_type": "timeout",
                    "error_message": "Request timed out",
                },
            ]
        },
    )

    connection = connect_database(database_path)
    stored_results = list_check_results_for_site(connection, site_id=site.id)
    connection.close()

    assert response.status_code == 200
    assert response.json() == {"accepted": 2}
    assert [result.probe_id for result in stored_results] == ["eu-dc-1", "eu-dc-1"]
    assert stored_results[0].checked_at == datetime(2026, 6, 19, 9, 30, tzinfo=UTC)
    assert stored_results[0].http_status == 200
    assert stored_results[0].status_group == "2xx"
    assert stored_results[0].result_status == "ok"
    assert stored_results[0].response_time_ms == 123
    assert stored_results[1].http_status is None
    assert stored_results[1].status_group == "network_error"
    assert stored_results[1].error_type == "timeout"
    assert stored_results[1].error_message == "Request timed out"


def test_results_submission_validates_payload(tmp_path) -> None:
    database_path = _build_database(tmp_path)
    connection = connect_database(database_path)
    create_probe(
        connection,
        probe_id="us-dc-1",
        name="United States Datacenter",
        region="United States",
        token_hash=hash_probe_token("probe-secret"),
    )
    connection.close()
    client = _build_client(database_path)

    response = client.post(
        "/api/probe/results",
        headers=_auth_headers("us-dc-1", "probe-secret"),
        json={"results": [{"site_id": 0, "checked_at": "not-a-date"}]},
    )

    assert response.status_code == 422


def test_results_submission_rejects_unknown_site(tmp_path) -> None:
    database_path = _build_database(tmp_path)
    connection = connect_database(database_path)
    create_probe(
        connection,
        probe_id="us-dc-1",
        name="United States Datacenter",
        region="United States",
        token_hash=hash_probe_token("probe-secret"),
    )
    connection.close()
    client = _build_client(database_path)

    response = client.post(
        "/api/probe/results",
        headers=_auth_headers("us-dc-1", "probe-secret"),
        json={
            "results": [
                {
                    "site_id": 999,
                    "checked_at": "2026-06-19T09:30:00+00:00",
                    "result_status": "ok",
                    "status_group": "2xx",
                    "http_status": 200,
                    "response_time_ms": 123,
                }
            ]
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Unknown site_id: 999"


def _build_database(tmp_path):
    database_path = tmp_path / "central.sqlite3"
    connection = connect_database(database_path)
    initialize_database(connection)
    connection.close()
    return database_path


def _build_client(database_path) -> TestClient:
    def override_database_connection():
        connection = connect_database(database_path)
        initialize_database(connection)
        yield connection
        connection.close()

    app.dependency_overrides[get_database_connection] = override_database_connection
    return TestClient(app)


def _auth_headers(probe_id: str, token: str) -> dict[str, str]:
    return {"X-Probe-Id": probe_id, "Authorization": f"Bearer {token}"}
