from datetime import UTC, date, datetime

from central.app.persistence import (
    get_probe,
    connect_database,
    create_check_result,
    create_probe,
    create_site,
    initialize_database,
    list_active_sites,
    list_check_results_for_site_on_date,
    list_check_results_for_site,
    list_recent_problem_results,
    seed_development_data,
)


def test_database_initializes_required_tables_and_indexes(tmp_path) -> None:
    database_path = tmp_path / "central.sqlite3"
    connection = connect_database(database_path)

    initialize_database(connection)

    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    indexes = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }

    assert {"sites", "probes", "check_results"}.issubset(tables)
    assert "idx_check_results_site_id" in indexes
    assert "idx_check_results_probe_id" in indexes
    assert "idx_check_results_checked_at" in indexes
    assert "idx_check_results_site_id_checked_at" in indexes


def test_persistence_flow_stores_http_check_result(tmp_path) -> None:
    connection = connect_database(tmp_path / "central.sqlite3")
    initialize_database(connection)

    site = create_site(
        connection,
        name="Example",
        url="https://example.com/",
    )
    probe = create_probe(
        connection,
        probe_id="ru-dc-1",
        name="Russia Datacenter",
        region="Russia",
        token_hash="test-token-hash",
    )
    checked_at = datetime(2026, 6, 19, 9, 30, tzinfo=UTC)

    result = create_check_result(
        connection,
        site_id=site.id,
        probe_id=probe.id,
        checked_at=checked_at,
        result_status="ok",
        status_group="2xx",
        http_status=200,
        response_time_ms=123,
    )

    stored_results = list_check_results_for_site(connection, site_id=site.id)

    assert site.enabled is True
    assert probe.probe_type == "datacenter"
    assert result.http_status == 200
    assert result.error_type is None
    assert stored_results == [result]


def test_persistence_flow_stores_network_error(tmp_path) -> None:
    connection = connect_database(tmp_path / "central.sqlite3")
    initialize_database(connection)

    site = create_site(
        connection,
        name="Unavailable",
        url="https://unavailable.example/",
    )
    probe = create_probe(
        connection,
        probe_id="eu-dc-1",
        name="Europe Datacenter",
        region="Europe",
        token_hash="test-token-hash",
    )

    result = create_check_result(
        connection,
        site_id=site.id,
        probe_id=probe.id,
        checked_at=datetime(2026, 6, 19, 9, 31, tzinfo=UTC),
        result_status="error",
        status_group="network_error",
        error_type="timeout",
        error_message="Request timed out",
    )

    assert result.http_status is None
    assert result.response_time_ms is None
    assert result.error_type == "timeout"


def test_dashboard_queries_filter_results_by_selected_date(tmp_path) -> None:
    connection = connect_database(tmp_path / "central.sqlite3")
    initialize_database(connection)
    site = create_site(connection, name="Example", url="https://example.com/")
    probe = create_probe(
        connection,
        probe_id="ru-dc-1",
        name="Russia Datacenter",
        region="Russia",
        token_hash="test-token-hash",
    )
    selected_day_ok = create_check_result(
        connection,
        site_id=site.id,
        probe_id=probe.id,
        checked_at=datetime(2026, 6, 19, 10, 0, tzinfo=UTC),
        result_status="ok",
        status_group="2xx",
        http_status=200,
        response_time_ms=120,
    )
    selected_day_problem = create_check_result(
        connection,
        site_id=site.id,
        probe_id=probe.id,
        checked_at=datetime(2026, 6, 19, 10, 1, tzinfo=UTC),
        result_status="server_error",
        status_group="5xx",
        http_status=500,
        response_time_ms=600,
    )
    create_check_result(
        connection,
        site_id=site.id,
        probe_id=probe.id,
        checked_at=datetime(2026, 6, 18, 10, 1, tzinfo=UTC),
        result_status="network_error",
        status_group="network_error",
        error_type="timeout",
    )

    daily_results = list_check_results_for_site_on_date(
        connection,
        site_id=site.id,
        selected_date=date(2026, 6, 19),
    )
    recent_problems = list_recent_problem_results(
        connection,
        site_id=site.id,
        selected_date=date(2026, 6, 19),
    )

    assert daily_results == [selected_day_ok, selected_day_problem]
    assert recent_problems == [selected_day_problem]


def test_development_seed_is_idempotent_and_has_no_real_secrets(tmp_path) -> None:
    connection = connect_database(tmp_path / "central.sqlite3")
    initialize_database(connection)

    seed_development_data(connection)
    seed_development_data(connection)

    sites = list_active_sites(connection)
    probes = connection.execute("SELECT * FROM probes ORDER BY id").fetchall()

    assert [site.url for site in sites] == ["https://example.com/"]
    assert [probe["id"] for probe in probes] == ["eu-dc-1", "ru-dc-1", "us-dc-1"]
    assert all(str(probe["token_hash"]) != f"dev-token-{probe['id']}" for probe in probes)
    assert len(get_probe(connection, "ru-dc-1").token_hash) == 64
