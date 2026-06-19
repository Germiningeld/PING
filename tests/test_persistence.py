from datetime import UTC, date, datetime, timedelta

from central.app.persistence import (
    count_problem_results_for_site_in_period,
    cleanup_check_results_older_than,
    get_probe,
    connect_database,
    create_check_result,
    create_probe,
    create_site,
    initialize_database,
    list_active_sites,
    list_check_results_for_site_on_date,
    list_check_results_for_site_in_period,
    list_check_details_for_site_in_period,
    list_check_results_for_site,
    list_recent_problem_results,
    list_problem_results_for_site_in_period,
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


def test_period_queries_use_half_open_utc_boundaries(tmp_path) -> None:
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
    start_at = datetime(2026, 6, 18, 21, 0, tzinfo=UTC)
    end_at = datetime(2026, 6, 19, 21, 0, tzinfo=UTC)

    create_check_result(
        connection,
        site_id=site.id,
        probe_id=probe.id,
        checked_at=start_at - timedelta(microseconds=1),
        result_status="server_error",
        status_group="5xx",
        http_status=500,
    )
    first = create_check_result(
        connection,
        site_id=site.id,
        probe_id=probe.id,
        checked_at=start_at,
        result_status="ok",
        status_group="2xx",
        http_status=200,
    )
    last_problem = create_check_result(
        connection,
        site_id=site.id,
        probe_id=probe.id,
        checked_at=end_at - timedelta(microseconds=1),
        result_status="network_error",
        status_group="network_error",
        error_type="timeout",
    )
    create_check_result(
        connection,
        site_id=site.id,
        probe_id=probe.id,
        checked_at=end_at,
        result_status="server_error",
        status_group="5xx",
        http_status=503,
    )

    results = list_check_results_for_site_in_period(
        connection,
        site_id=site.id,
        start_at=start_at,
        end_at=end_at,
    )
    problems = list_problem_results_for_site_in_period(
        connection,
        site_id=site.id,
        start_at=start_at,
        end_at=end_at,
    )

    assert results == [first, last_problem]
    assert problems == [last_problem]


def test_detail_query_is_newest_first_and_limited_without_limiting_period_data(
    tmp_path,
) -> None:
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
    start_at = datetime(2026, 6, 19, 9, 0, tzinfo=UTC)
    created = [
        create_check_result(
            connection,
            site_id=site.id,
            probe_id=probe.id,
            checked_at=start_at + timedelta(minutes=index),
            result_status="ok",
            status_group="2xx",
            http_status=200,
        )
        for index in range(3)
    ]

    all_results = list_check_results_for_site_in_period(
        connection,
        site_id=site.id,
        start_at=start_at,
        end_at=start_at + timedelta(minutes=3),
    )
    details = list_check_details_for_site_in_period(
        connection,
        site_id=site.id,
        start_at=start_at,
        end_at=start_at + timedelta(minutes=3),
        limit=2,
    )

    assert all_results == created
    assert details == [created[2], created[1]]


def test_problem_count_is_independent_of_display_limit_and_results_are_newest_first(
    tmp_path,
) -> None:
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
    start_at = datetime(2026, 6, 19, 9, 0, tzinfo=UTC)
    created = [
        create_check_result(
            connection,
            site_id=site.id,
            probe_id=probe.id,
            checked_at=start_at + timedelta(minutes=index),
            result_status="server_error",
            status_group="5xx",
            http_status=500,
        )
        for index in range(12)
    ]

    displayed = list_problem_results_for_site_in_period(
        connection,
        site_id=site.id,
        start_at=start_at,
        end_at=start_at + timedelta(minutes=12),
    )
    total = count_problem_results_for_site_in_period(
        connection,
        site_id=site.id,
        start_at=start_at,
        end_at=start_at + timedelta(minutes=12),
    )

    assert len(displayed) == 10
    assert displayed == list(reversed(created[2:]))
    assert total == 12


def test_cleanup_check_results_older_than_deletes_only_expired_raw_results(tmp_path) -> None:
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
    expired_result = create_check_result(
        connection,
        site_id=site.id,
        probe_id=probe.id,
        checked_at=datetime(2026, 3, 20, 11, 59, 59, tzinfo=UTC),
        result_status="ok",
        status_group="2xx",
        http_status=200,
        response_time_ms=100,
    )
    boundary_result = create_check_result(
        connection,
        site_id=site.id,
        probe_id=probe.id,
        checked_at=datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
        result_status="ok",
        status_group="2xx",
        http_status=200,
        response_time_ms=110,
    )
    fresh_result = create_check_result(
        connection,
        site_id=site.id,
        probe_id=probe.id,
        checked_at=datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC),
        result_status="ok",
        status_group="2xx",
        http_status=200,
        response_time_ms=120,
    )

    deleted_count = cleanup_check_results_older_than(
        connection,
        cutoff=datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
    )
    stored_results = list_check_results_for_site(connection, site_id=site.id)

    assert deleted_count == 1
    assert [result.id for result in stored_results] == [
        boundary_result.id,
        fresh_result.id,
    ]
    assert expired_result.id not in [result.id for result in stored_results]
    assert get_probe(connection, "ru-dc-1") == probe


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
