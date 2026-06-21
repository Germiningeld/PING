from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from central.app.auth import ADMIN_SESSION_COOKIE, hash_admin_password
from central.app.dashboard import (
    DashboardFilters,
    DashboardPeriod,
    DEFAULT_DETAIL_LIMIT,
    DETAIL_LIMITS,
    _build_response_time_chart_view,
    _parse_dashboard_filters,
    _parse_dashboard_period,
    _parse_detail_limit,
    _parse_detail_page,
    _probe_colors,
    _render_probe_period_summary,
    _render_response_time_chart,
    _summarize_probe_period,
)
from central.app.main import app
from central.app.models import CheckResult, Probe
from central.app.persistence import (
    connect_database,
    create_check_result,
    create_probe,
    create_site,
    initialize_database,
)
from central.app.probe_api import get_database_connection


def _probe(probe_id: str) -> Probe:
    created_at = datetime(2026, 6, 19, tzinfo=UTC)
    return Probe(
        id=probe_id,
        name=f"Probe {probe_id}",
        region="Test",
        probe_type="datacenter",
        network_label="Test network",
        enabled=True,
        token_hash="hash",
        created_at=created_at,
        updated_at=created_at,
    )


def _result(
    result_id: int,
    *,
    probe_id: str,
    checked_at: datetime,
    status_group: str,
    response_time_ms: int | None = None,
) -> CheckResult:
    return CheckResult(
        id=result_id,
        site_id=1,
        probe_id=probe_id,
        checked_at=checked_at,
        result_status="ok" if status_group == "2xx" else status_group,
        status_group=status_group,
        http_status=200 if status_group == "2xx" else None,
        response_time_ms=response_time_ms,
        error_type=status_group if "error" in status_group else None,
        error_message=None,
        created_at=checked_at,
    )


def _chart_period(start_at: datetime, *, minutes: int = 10) -> DashboardPeriod:
    end_at = start_at + timedelta(minutes=minutes)
    return DashboardPeriod(
        selected_date=start_at.astimezone(ZoneInfo("Europe/Moscow")).date(),
        from_time=start_at.astimezone(ZoneInfo("Europe/Moscow")).time(),
        to_time=(end_at - timedelta(minutes=1)).astimezone(
            ZoneInfo("Europe/Moscow")
        ).time(),
        start_at=start_at,
        end_at=end_at,
    )


@pytest.fixture
def dashboard_client(tmp_path, monkeypatch):
    database_path = tmp_path / "central.sqlite3"
    connection = connect_database(database_path)
    initialize_database(connection)
    site = create_site(connection, name="Example", url="https://example.com/")
    selected_date = datetime.now(ZoneInfo("Europe/Moscow")).date()
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
    monkeypatch.delenv("PING_ENV", raising=False)
    monkeypatch.delenv("PING_AUTH_DISABLED", raising=False)

    def override_database_connection():
        connection = connect_database(database_path)
        initialize_database(connection)
        yield connection
        connection.close()

    app.dependency_overrides[get_database_connection] = override_database_connection
    try:
        client = TestClient(app)
        client.database_path = database_path
        yield client
    finally:
        app.dependency_overrides.pop(get_database_connection, None)


def test_dashboard_redirects_unauthenticated_user_to_login(dashboard_client) -> None:
    response = dashboard_client.get("/dashboard", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_local_development_mode_bypasses_dashboard_auth(
    dashboard_client, monkeypatch
) -> None:
    monkeypatch.setenv("PING_ENV", "development")
    monkeypatch.setenv("PING_AUTH_DISABLED", "true")

    root_response = dashboard_client.get("/", follow_redirects=False)
    dashboard_response = dashboard_client.get("/dashboard", follow_redirects=False)
    login_response = dashboard_client.get("/login", follow_redirects=False)
    login_submit_response = dashboard_client.post("/login", follow_redirects=False)
    logout_response = dashboard_client.post("/logout", follow_redirects=False)

    assert root_response.status_code == 303
    assert root_response.headers["location"] == "/dashboard"
    assert dashboard_response.status_code == 200
    assert "PING Dashboard" in dashboard_response.text
    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/dashboard"
    assert login_submit_response.status_code == 303
    assert login_submit_response.headers["location"] == "/dashboard"
    assert logout_response.status_code == 303
    assert logout_response.headers["location"] == "/dashboard"


@pytest.mark.parametrize("ping_env", ["production", "staging", ""])
def test_auth_disabled_flag_is_fail_closed_outside_development(
    dashboard_client, monkeypatch, ping_env
) -> None:
    monkeypatch.setenv("PING_ENV", ping_env)
    monkeypatch.setenv("PING_AUTH_DISABLED", "true")

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
    assert "История времени ответа" in dashboard_response.text
    assert "aria-label=\"Время ответа по probes за выбранный период\"" in dashboard_response.text
    assert 'data-probe-toggle="ru-dc-1"' in dashboard_response.text
    assert 'data-probe-toggle="eu-dc-1"' in dashboard_response.text
    assert "503" in dashboard_response.text


def test_dashboard_renders_compact_localized_summary_without_site_report_header(
    dashboard_client,
) -> None:
    dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )

    response = dashboard_client.get("/dashboard")

    assert response.status_code == 200
    assert "<h1>Example</h1>" not in response.text
    assert '<p class="muted">https://example.com</p>' not in response.text
    assert "Сводка по probes" in response.text
    assert "Последняя проверка" in response.text
    assert "Среднее время" in response.text
    assert "Получено" in response.text
    assert "Покрытие" in response.text
    assert "Last Status" not in response.text
    assert "Last HTTP/Error" not in response.text
    assert '<div class="status-key">' not in response.text
    assert '<a class="brand-link" href="/dashboard"><strong>PING — мониторинг</strong></a>' in (
        response.text
    )


def test_dashboard_status_badges_use_distinct_5xx_and_network_error_styles(
    dashboard_client,
) -> None:
    dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )
    dashboard_response = dashboard_client.get("/dashboard")
    stylesheet_response = dashboard_client.get("/assets/css/dashboard.css")

    assert dashboard_response.status_code == 200
    assert stylesheet_response.status_code == 200
    assert stylesheet_response.headers["content-type"].startswith("text/css")
    assert (
        ".status-5xx { background: #ffccc7; color: #820014; }"
        in stylesheet_response.text
    )
    assert (
        ".status-network_error { background: #5c0011; color: #fff1f0; }"
        in stylesheet_response.text
    )
    assert ".status-5xx, .status-network_error" not in stylesheet_response.text
    assert '<link rel="stylesheet" href="/assets/bootstrap.min.css">' in (
        dashboard_response.text
    )
    assert dashboard_response.text.index("/assets/bootstrap.min.css") < (
        dashboard_response.text.index("/assets/css/dashboard.css")
    )
    assert '<span class="status badge status-5xx">5xx</span>' in dashboard_response.text
    assert (
        '<span class="status badge status-network_error">network_error</span>'
        in dashboard_response.text
    )


def test_dashboard_can_show_selected_date_history(dashboard_client) -> None:
    dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )
    selected_date = (
        datetime.now(ZoneInfo("Europe/Moscow")).date() - timedelta(days=1)
    ).isoformat()
    dashboard_response = dashboard_client.get(f"/dashboard?date={selected_date}")

    assert dashboard_response.status_code == 200
    assert f'value="{selected_date}"' in dashboard_response.text
    assert "network_error" in dashboard_response.text
    assert "timeout" in dashboard_response.text
    assert 'class="event-strip-line"' in dashboard_response.text
    assert 'data-status-group="network_error"' in dashboard_response.text


def test_dashboard_date_navigation_preserves_url_and_chart_filter_state(
    dashboard_client,
) -> None:
    dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )
    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    selected_date = today - timedelta(days=1)

    response = dashboard_client.get(
        "/dashboard",
        params={
            "site_id": 1,
            "date": selected_date.isoformat(),
            "from_time": "08:15",
            "to_time": "17:45",
            "limit": "100",
            "probes": "ru-dc-1",
            "statuses": "2xx,5xx",
        },
    )
    assert response.status_code == 200
    for target_date, label in (
        (selected_date - timedelta(days=1), "‹"),
        (today, "Сегодня"),
        (selected_date + timedelta(days=1), "›"),
    ):
        expected_href = (
            f"/dashboard?date={target_date.isoformat()}&amp;from_time=08%3A15"
            "&amp;to_time=17%3A45&amp;limit=100&amp;probes=ru-dc-1"
            "&amp;statuses=2xx%2C5xx&amp;site_id=1&amp;page=1"
        )
        assert expected_href in response.text
        assert f">{label}</a>" in response.text
    assert "ping-dashboard-chart-filters-v1" in response.text


def test_dashboard_date_navigation_disables_retention_and_future_boundaries(
    dashboard_client,
) -> None:
    dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )
    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    retention_boundary = today - timedelta(days=89)

    today_response = dashboard_client.get("/dashboard")
    boundary_response = dashboard_client.get(
        "/dashboard", params={"date": retention_boundary.isoformat()}
    )

    assert (
        'aria-label="Перейти к сегодняшнему дню">Сегодня</span>'
        in today_response.text
    )
    assert (
        'aria-label="Следующий день">›</span>'
        in today_response.text
    )
    assert (
        'aria-label="Предыдущий день">‹</span>'
        in boundary_response.text
    )
    assert f'min="{retention_boundary.isoformat()}"' in boundary_response.text
    assert f'max="{today.isoformat()}"' in boundary_response.text


def test_dashboard_auto_refreshes_only_today_without_sticky_header(
    dashboard_client,
) -> None:
    dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )
    yesterday = datetime.now(ZoneInfo("Europe/Moscow")).date() - timedelta(days=1)

    today_response = dashboard_client.get("/dashboard")
    historical_response = dashboard_client.get(
        "/dashboard", params={"date": yesterday.isoformat()}
    )

    refresh_script = "window.setTimeout(() => window.location.reload(), 60_000);"
    assert refresh_script in today_response.text
    assert refresh_script not in historical_response.text
    assert "position: sticky" not in today_response.text


def test_dashboard_period_converts_full_msk_day_to_utc() -> None:
    period = _parse_dashboard_period(
        date_value="2026-06-19",
        from_time_value="00:00",
        to_time_value="23:59",
        min_date=datetime(2026, 1, 1, tzinfo=UTC).date(),
        max_date=datetime(2026, 6, 19, tzinfo=UTC).date(),
    )

    assert period.start_at == datetime(2026, 6, 18, 21, 0, tzinfo=UTC)
    assert period.end_at == datetime(2026, 6, 19, 21, 0, tzinfo=UTC)
    assert period.message is None


def test_dashboard_period_converts_selected_msk_minutes_to_utc() -> None:
    period = _parse_dashboard_period(
        date_value="2026-06-19",
        from_time_value="12:30",
        to_time_value="14:45",
        min_date=datetime(2026, 1, 1, tzinfo=UTC).date(),
        max_date=datetime(2026, 6, 19, tzinfo=UTC).date(),
    )

    assert period.start_at == datetime(2026, 6, 19, 9, 30, tzinfo=UTC)
    assert period.end_at == datetime(2026, 6, 19, 11, 46, tzinfo=UTC)


def test_dashboard_period_limits_date_to_retention_window() -> None:
    min_date = datetime(2026, 3, 22, tzinfo=UTC).date()
    max_date = datetime(2026, 6, 19, tzinfo=UTC).date()

    future = _parse_dashboard_period(
        date_value="2026-06-20",
        from_time_value=None,
        to_time_value=None,
        min_date=min_date,
        max_date=max_date,
    )
    expired = _parse_dashboard_period(
        date_value="2026-03-21",
        from_time_value=None,
        to_time_value=None,
        min_date=min_date,
        max_date=max_date,
    )

    assert future.selected_date == max_date
    assert expired.selected_date == min_date
    assert future.message == "Дата ограничена доступным периодом хранения."
    assert expired.message == "Дата ограничена доступным периодом хранения."


def test_dashboard_safely_normalizes_invalid_period_parameters(
    dashboard_client,
) -> None:
    dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )

    response = dashboard_client.get(
        "/dashboard?date=not-a-date&from_time=18:00&to_time=09:00"
    )

    assert response.status_code == 200
    assert 'name="from_time" type="time" value="00:00"' in response.text
    assert 'name="to_time" type="time" value="23:59"' in response.text
    assert "Некорректная дата заменена сегодняшней." in response.text
    assert "From не может быть позже To" in response.text


def test_dashboard_period_filters_graph_and_details(dashboard_client) -> None:
    dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )
    selected_date = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()

    response = dashboard_client.get(
        f"/dashboard?date={selected_date}&from_time=00:00&to_time=00:00"
    )

    assert response.status_code == 200
    assert "Нет данных о времени ответа за выбранный период." in response.text
    assert "Нет результатов проверок за выбранный период." in response.text
    assert "Проблемы за выбранный период" not in response.text
    assert f"date={selected_date}&amp;from_time=00%3A00&amp;to_time=00%3A00" in response.text


def test_dashboard_detail_limit_uses_allowlist_and_is_preserved_in_navigation(
    dashboard_client,
) -> None:
    dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )

    selected = dashboard_client.get("/dashboard?limit=20")
    invalid = dashboard_client.get("/dashboard?limit=21")
    malformed = dashboard_client.get("/dashboard?limit=many")

    assert selected.status_code == 200
    assert '<option\n                                value="20" selected>20' in selected.text
    assert "limit=20" in selected.text
    assert f'value="{DEFAULT_DETAIL_LIMIT}" selected' in invalid.text
    assert f'value="{DEFAULT_DETAIL_LIMIT}" selected' in malformed.text
    assert tuple(_parse_detail_limit(str(value)) for value in DETAIL_LIMITS) == DETAIL_LIMITS
    assert _parse_detail_limit(None) == DEFAULT_DETAIL_LIMIT
    assert _parse_detail_page(None) == 1
    assert _parse_detail_page("2") == 2
    assert _parse_detail_page("0") == 1
    assert _parse_detail_page("invalid") == 1


def test_dashboard_serves_vendored_bootstrap_asset(dashboard_client) -> None:
    response = dashboard_client.get("/assets/bootstrap.min.css")
    project_css = dashboard_client.get("/assets/css/dashboard.css")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert "Bootstrap  v5.3.8" in response.text
    assert project_css.status_code == 200
    assert project_css.headers["content-type"].startswith("text/css")
    assert ".chart-panel" in project_css.text
    assert ".legend-item, .status-toggle" in project_css.text
    assert ".legend-item { padding: 5px 8px;" in project_css.text
    assert ".status-toggle { width: max-content; padding: 0;" in project_css.text
    assert ".status-toggle .status { min-width: 0; padding: 6px 20px; }" in (
        project_css.text
    )


def test_dashboard_details_pagination_preserves_period_and_does_not_limit_chart(
    dashboard_client,
) -> None:
    connection = connect_database(dashboard_client.database_path)
    selected_date = datetime.now(ZoneInfo("Europe/Moscow")).date()
    start_at = datetime.combine(selected_date, time(12, 0), tzinfo=UTC)
    for index in range(43):
        create_check_result(
            connection,
            site_id=1,
            probe_id="ru-dc-1",
            checked_at=start_at + timedelta(minutes=index),
            result_status="ok",
            status_group="2xx",
            http_status=200,
            response_time_ms=1000 + index,
        )
    connection.close()
    dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )
    parameters = {
        "site_id": 1,
        "date": selected_date.isoformat(),
        "from_time": "00:00",
        "to_time": "23:59",
        "limit": 20,
    }

    first = dashboard_client.get("/dashboard", params=parameters)
    middle = dashboard_client.get("/dashboard", params={**parameters, "page": 2})
    last = dashboard_client.get("/dashboard", params={**parameters, "page": 3})
    empty = dashboard_client.get("/dashboard", params={**parameters, "page": 4})

    assert "Страница 1 из 3; всего" in first.text and "результатов: 45" in first.text
    assert "Страница 2 из 3; всего" in middle.text and "результатов: 45" in middle.text
    assert "Страница 3 из 3; всего" in last.text and "результатов: 45" in last.text
    assert "Страница 4 из 3; всего" in empty.text and "результатов: 45" in empty.text
    assert "Нет результатов проверок за выбранный период." in empty.text
    assert 'data-response-chart' in empty.text
    assert 'from_time=00%3A00&amp;to_time=23%3A59&amp;limit=20' in middle.text
    assert 'page=1' in middle.text and 'page=3' in middle.text
    details_html = first.text.split("<h2>Детали проверок</h2>", 1)[1]
    assert 'name="page"' not in details_html


def test_dashboard_limit_form_is_below_details_and_preserves_period_and_filters(
    dashboard_client,
) -> None:
    dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )
    selected_date = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()

    response = dashboard_client.get(
        "/dashboard",
        params={
            "site_id": 1,
            "date": selected_date,
            "from_time": "08:15",
            "to_time": "17:45",
            "limit": "100",
            "probes": "ru-dc-1",
            "statuses": "2xx,5xx",
        },
    )

    details_heading = response.text.index("<h2>Детали проверок</h2>")
    details_table = response.text.index("<table class=", details_heading)
    details_form = response.text.index('<form class="limit-form"', details_table)
    assert details_heading < details_table < details_form
    assert "details-toolbar" not in response.text
    assert "details_from_time" not in response.text
    assert "details_to_time" not in response.text
    assert response.text.count('name="from_time" type="time"') == 1
    assert response.text.count('name="to_time" type="time"') == 1
    assert '<input type="hidden" name="site_id" value="1">' in response.text
    assert 'name="date"' in response.text and f'value="{selected_date}"' in response.text
    assert 'name="from_time" value="08:15"' in response.text
    assert 'name="to_time"\n                            value="17:45"' in response.text
    assert 'name="probes"\n                            value="ru-dc-1"' in response.text
    assert 'name="statuses"\n                            value="2xx,5xx"' in response.text
    assert 'value="100" selected' in response.text


def test_probe_period_summary_calculates_metrics_and_caps_coverage() -> None:
    start_at = datetime(2026, 6, 19, 9, 0, tzinfo=UTC)
    period = DashboardPeriod(
        selected_date=start_at.date(),
        from_time=start_at.time(),
        to_time=(start_at + timedelta(minutes=4)).time(),
        start_at=start_at,
        end_at=start_at + timedelta(minutes=5),
    )
    status_groups = ("2xx", "3xx", "4xx", "5xx", "network_error", "probe_error")
    response_times = (100, 200, 300, 500, None, None)
    results = [
        _result(
            index,
            probe_id="probe-a",
            checked_at=start_at + timedelta(seconds=index),
            status_group=status_group,
            response_time_ms=response_time,
        )
        for index, (status_group, response_time) in enumerate(
            zip(status_groups, response_times), start=1
        )
    ]

    summaries, overall_uptime = _summarize_probe_period(
        [_probe("probe-a")], results, period=period
    )

    summary = summaries[0]
    assert summary.average_response_time_ms == pytest.approx(275)
    assert summary.uptime_percent == pytest.approx(100 / 6)
    assert summary.received_checks == 6
    assert summary.coverage_percent == 100
    assert summary.status_counts == {status_group: 1 for status_group in status_groups}
    assert overall_uptime == pytest.approx(100 / 6)


def test_probe_period_summary_handles_empty_period_and_errors_without_response_time() -> None:
    start_at = datetime(2026, 6, 19, 9, 0, tzinfo=UTC)
    period = DashboardPeriod(
        selected_date=start_at.date(),
        from_time=start_at.time(),
        to_time=(start_at + timedelta(minutes=9)).time(),
        start_at=start_at,
        end_at=start_at + timedelta(minutes=10),
    )
    results = [
        _result(
            1,
            probe_id="probe-errors",
            checked_at=start_at,
            status_group="network_error",
        ),
        _result(
            2,
            probe_id="probe-errors",
            checked_at=start_at + timedelta(minutes=1),
            status_group="probe_error",
        ),
    ]

    summaries, overall_uptime = _summarize_probe_period(
        [_probe("probe-empty"), _probe("probe-errors")], results, period=period
    )

    empty, errors = summaries
    assert empty.received_checks == 0
    assert empty.average_response_time_ms is None
    assert empty.uptime_percent is None
    assert empty.coverage_percent == 0
    assert errors.received_checks == 2
    assert errors.average_response_time_ms is None
    assert errors.uptime_percent == 0
    assert errors.coverage_percent == 20
    assert overall_uptime == 0


def test_probe_stale_threshold_is_strictly_older_than_three_minutes() -> None:
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    period = DashboardPeriod(
        selected_date=now.date(),
        from_time=time(11, 0),
        to_time=time(11, 59),
        start_at=now - timedelta(hours=1),
        end_at=now,
    )
    probes = [_probe("fresh"), _probe("stale")]
    latest_results = {
        "fresh": _result(
            1,
            probe_id="fresh",
            checked_at=now - timedelta(minutes=3),
            status_group="2xx",
        ),
        "stale": _result(
            2,
            probe_id="stale",
            checked_at=now - timedelta(minutes=3, microseconds=1),
            status_group="2xx",
        ),
    }

    html = _render_probe_period_summary(
        probes, latest_results, [], period=period, now=now
    )

    assert html.count("text-bg-danger") == 1
    assert "Общий uptime: нет данных за выбранный период." in html


def test_probe_chart_colors_are_unique_stable_and_not_tied_to_demo_ids() -> None:
    probes = [_probe("central"), _probe("usa"), _probe("eu"), _probe("custom-probe")]

    colors = _probe_colors(probes)
    reordered_colors = _probe_colors(list(reversed(probes)))

    assert colors == reordered_colors
    assert len(set(colors.values())) == len(probes)
    assert all(color.startswith("hsl(") for color in colors.values())


def test_response_chart_renders_filters_problem_markers_and_error_strip() -> None:
    start_at = datetime(2026, 6, 19, 9, 0, tzinfo=UTC)
    probes = [_probe("central"), _probe("usa")]
    results = [
        _result(
            1,
            probe_id="central",
            checked_at=start_at,
            status_group="2xx",
            response_time_ms=100,
        ),
        _result(
            2,
            probe_id="central",
            checked_at=start_at + timedelta(minutes=1),
            status_group="5xx",
            response_time_ms=500,
        ),
        _result(
            3,
            probe_id="central",
            checked_at=start_at + timedelta(minutes=2),
            status_group="network_error",
        ),
    ]

    html = _render_response_time_chart(results, probes, _chart_period(start_at))

    assert 'data-response-chart' in html
    assert html.count('data-probe-toggle=') == 2
    assert html.count('data-status-toggle=') == 6
    assert 'data-status-group="5xx"' in html
    assert 'point-active problem-marker status-point-5xx' in html
    assert 'class="problem-marker status-point-network_error"' in html
    assert 'class="point-hit"' in html
    assert 'class="point-active point-ok"' in html
    assert 'r="8" tabindex="0"' in html
    assert 'r="2"' in html
    assert 'tabindex="0"' in html and 'aria-label="' in html
    assert 'style="--probe-color:' in html
    assert html.index('<div class="chart-plot">') < html.index('<div class="legend"')
    assert '<input type="checkbox"' not in html
    assert 'data-status-toggle="2xx" aria-pressed="true"' in html
    assert html.count('class="series-segment"') == 1
    assert ">12:00 MSK</text>" in html
    assert ">12:09 MSK</text>" in html
    assert 'cx="56.0"' in html


def test_response_chart_does_not_connect_hidden_results_or_large_gaps() -> None:
    start_at = datetime(2026, 6, 19, 9, 0, tzinfo=UTC)
    results = [
        _result(
            1,
            probe_id="central",
            checked_at=start_at,
            status_group="2xx",
            response_time_ms=100,
        ),
        _result(
            2,
            probe_id="central",
            checked_at=start_at + timedelta(minutes=1),
            status_group="4xx",
            response_time_ms=400,
        ),
        _result(
            3,
            probe_id="central",
            checked_at=start_at + timedelta(minutes=2),
            status_group="probe_error",
        ),
        _result(
            4,
            probe_id="central",
            checked_at=start_at + timedelta(minutes=3),
            status_group="2xx",
            response_time_ms=120,
        ),
        _result(
            5,
            probe_id="central",
            checked_at=start_at + timedelta(minutes=6),
            status_group="2xx",
            response_time_ms=130,
        ),
    ]

    html = _render_response_time_chart(
        results, [_probe("central")], _chart_period(start_at)
    )

    assert html.count('class="series-segment"') == 1
    assert 'data-statuses="2xx 4xx"' in html
    assert 'data-statuses="4xx 2xx"' not in html
    assert 'data-statuses="2xx 2xx"' not in html


def test_response_chart_filters_persist_for_future_page_refreshes(
    dashboard_client,
) -> None:
    dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )
    response = dashboard_client.get(
        "/dashboard?probes=ru-dc-1&statuses=2xx,5xx"
    )

    assert 'data-status-toggle="2xx" aria-pressed="true"' in response.text
    assert 'data-status-toggle="3xx" aria-pressed="false"' in response.text
    assert 'data-probe-toggle="eu-dc-1" aria-pressed="false"' in response.text
    assert 'name="probes"\n                    value="ru-dc-1"' in response.text
    assert 'name="statuses" value="2xx,5xx"' in response.text
    assert "ping-dashboard-chart-filters-v1" in response.text
    assert "localStorage.setItem(storageKey" in response.text
    assert 'url.searchParams.set(parameter, enabled.join(","))' in response.text
    assert 'url.searchParams.delete("page")' in response.text


def test_dashboard_filters_details_but_keeps_full_probe_summary(
    dashboard_client,
) -> None:
    dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )

    filtered = dashboard_client.get(
        "/dashboard?probes=ru-dc-1&statuses=2xx"
    )
    summary_html = filtered.text.split("<h2>Сводка по probes</h2>", 1)[1].split(
        "<h2>История времени ответа</h2>", 1
    )[0]
    details_html = filtered.text.split("<h2>Детали проверок</h2>", 1)[1]

    assert "Russia Datacenter" in summary_html
    assert "Europe Datacenter" in summary_html
    assert "<th>2xx</th>" in summary_html
    assert "<th>5xx</th>" in summary_html
    assert "123 ms" in details_html
    assert "503" not in details_html
    assert "результатов: 1" in details_html
    assert "Проблемы за выбранный период" not in filtered.text


def test_chart_y_scale_uses_only_enabled_probe_and_status_points() -> None:
    start_at = datetime(2026, 6, 19, 9, 0, tzinfo=UTC)
    probes = [_probe("slow"), _probe("fast")]
    results = [
        _result(
            1,
            probe_id="slow",
            checked_at=start_at,
            status_group="2xx",
            response_time_ms=1000,
        ),
        _result(
            2,
            probe_id="fast",
            checked_at=start_at,
            status_group="2xx",
            response_time_ms=100,
        ),
        _result(
            3,
            probe_id="fast",
            checked_at=start_at + timedelta(minutes=1),
            status_group="5xx",
            response_time_ms=700,
        ),
    ]

    fast_success = _build_response_time_chart_view(
        results,
        probes,
        _chart_period(start_at),
        DashboardFilters(probe_ids=("fast",), status_groups=("2xx",)),
    )
    no_visible_points = _build_response_time_chart_view(
        results,
        probes,
        _chart_period(start_at),
        DashboardFilters(probe_ids=(), status_groups=("2xx",)),
    )

    assert fast_success["max_response"] == 110
    assert fast_success["has_visible_response_data"] is True
    assert no_visible_points["max_response"] == 100
    assert no_visible_points["has_visible_response_data"] is False


def test_dashboard_time_controls_are_single_bounded_and_non_wrapping(
    dashboard_client,
) -> None:
    dashboard_client.post(
        "/login",
        data={"username": "admin", "password": "correct-password"},
        follow_redirects=False,
    )
    response = dashboard_client.get("/dashboard")

    assert response.text.count('name="from_time" type="time"') == 1
    assert response.text.count('name="to_time" type="time"') == 1
    assert response.text.count('min="00:00" max="23:59" step="60"') == 2
    assert "Math.min(1439, Math.max(0, current + delta))" in response.text
    assert "event.stopImmediatePropagation();" in response.text
    assert "{ passive: false, capture: true }" in response.text
    assert "window.requestAnimationFrame(() => { input.value = formatMinutes(next); });" in (
        response.text
    )
    assert 'event.key === "ArrowUp" ? 1 : -1' in response.text
    assert "event.deltaY < 0 ? 1 : -1" in response.text


def test_dashboard_filter_parser_allowlists_values_and_supports_empty_selection() -> None:
    probes = [_probe("central"), _probe("eu")]

    parsed = _parse_dashboard_filters(
        "eu,unknown", "5xx,unknown,2xx", probes=probes
    )
    empty = _parse_dashboard_filters("", "", probes=probes)

    assert parsed == DashboardFilters(
        probe_ids=("eu",), status_groups=("2xx", "5xx")
    )
    assert empty == DashboardFilters(probe_ids=(), status_groups=())


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
