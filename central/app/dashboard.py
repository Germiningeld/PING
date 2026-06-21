from __future__ import annotations

import sqlite3
from hashlib import sha256
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from math import ceil
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from central.app.auth import (
    ADMIN_SESSION_COOKIE,
    ADMIN_SESSION_MAX_AGE_SECONDS,
    LOCAL_ADMIN_USERNAME,
    admin_auth_disabled,
    admin_auth_configured,
    cookie_secure_enabled,
    create_admin_session_token,
    verify_admin_credentials,
    verify_admin_session_token,
)
from central.app.models import CheckResult, Probe, Site
from central.app.persistence import (
    count_check_results_for_site_in_period,
    get_site,
    list_active_sites,
    list_check_details_for_site_in_period,
    list_check_results_for_site_in_period,
    list_enabled_probes,
    list_latest_results_for_site_by_probe,
    seed_development_data,
)
from central.app.probe_api import DatabaseConnection


router = APIRouter(tags=["dashboard"])
RETENTION_DAYS = 90
STATUS_GROUPS = ("2xx", "3xx", "4xx", "5xx", "network_error", "probe_error")
MSK = ZoneInfo("Europe/Moscow")
DEFAULT_FROM_TIME = time(0, 0)
DEFAULT_TO_TIME = time(23, 59)
PROBE_INTERVAL_SECONDS = 60
STALE_AFTER = timedelta(minutes=3)
DETAIL_LIMITS = (20, 50, 100, 250)
DEFAULT_DETAIL_LIMIT = 50
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
TEMPLATE_DIR = APP_DIR / "templates"
templates = Jinja2Templates(directory=TEMPLATE_DIR)


@dataclass(frozen=True)
class DashboardPeriod:
    selected_date: date
    from_time: time
    to_time: time
    start_at: datetime
    end_at: datetime
    message: str | None = None


@dataclass(frozen=True)
class DashboardFilters:
    probe_ids: tuple[str, ...]
    status_groups: tuple[str, ...]

    @property
    def probe_query(self) -> str:
        return ",".join(self.probe_ids)

    @property
    def status_query(self) -> str:
        return ",".join(self.status_groups)


@dataclass(frozen=True)
class ProbePeriodSummary:
    probe_id: str
    average_response_time_ms: float | None
    uptime_percent: float | None
    received_checks: int
    coverage_percent: float
    status_counts: dict[str, int]


@router.get("/", response_class=HTMLResponse)
def root(request: Request) -> Response:
    if _current_admin_username(request) is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/login", response_class=HTMLResponse)
def login_screen(request: Request) -> Response:
    if admin_auth_disabled():
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    if _current_admin_username(request) is not None:
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login(request: Request) -> Response:
    if admin_auth_disabled():
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    username = form.get("username", [""])[0]
    password = form.get("password", [""])[0]

    if not admin_auth_configured():
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Admin credentials are not configured."},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    if not verify_admin_credentials(username=username, password=password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid username or password."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    response = RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        create_admin_session_token(username),
        max_age=ADMIN_SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=cookie_secure_enabled(),
        samesite="lax",
    )
    return response


@router.post("/logout")
def logout() -> Response:
    redirect_target = "/dashboard" if admin_auth_disabled() else "/login"
    response = RedirectResponse(redirect_target, status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(
        ADMIN_SESSION_COOKIE,
        httponly=True,
        secure=cookie_secure_enabled(),
        samesite="lax",
    )
    return response


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    connection: DatabaseConnection,
    site_id: Annotated[int | None, Query(gt=0)] = None,
    date_value: Annotated[str | None, Query(alias="date")] = None,
    from_time: Annotated[str | None, Query()] = None,
    to_time: Annotated[str | None, Query()] = None,
    limit: Annotated[str | None, Query()] = None,
    page: Annotated[str | None, Query()] = None,
    probe_filter: Annotated[str | None, Query(alias="probes")] = None,
    status_filter: Annotated[str | None, Query(alias="statuses")] = None,
) -> Response:
    username = _current_admin_username(request)
    if username is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    _ensure_development_seed(connection)
    sites = list_active_sites(connection)
    selected_site = _select_site(connection, sites=sites, site_id=site_id)
    probes = list_enabled_probes(connection)
    today = datetime.now(MSK).date()
    min_date = today - timedelta(days=RETENTION_DAYS - 1)
    period = _parse_dashboard_period(
        date_value=date_value,
        from_time_value=from_time,
        to_time_value=to_time,
        min_date=min_date,
        max_date=today,
    )
    detail_limit = _parse_detail_limit(limit)
    detail_page = _parse_detail_page(page)
    filters = _parse_dashboard_filters(
        probe_filter,
        status_filter,
        probes=probes,
    )
    latest_results = (
        list_latest_results_for_site_by_probe(connection, site_id=selected_site.id)
        if selected_site is not None
        else {}
    )
    period_results = (
        list_check_results_for_site_in_period(
            connection,
            site_id=selected_site.id,
            start_at=period.start_at,
            end_at=period.end_at,
        )
        if selected_site is not None
        else []
    )
    detail_results = (
        list_check_details_for_site_in_period(
            connection,
            site_id=selected_site.id,
            start_at=period.start_at,
            end_at=period.end_at,
            limit=detail_limit,
            offset=(detail_page - 1) * detail_limit,
            probe_ids=filters.probe_ids,
            status_groups=filters.status_groups,
        )
        if selected_site is not None
        else []
    )
    detail_count = (
        count_check_results_for_site_in_period(
            connection,
            site_id=selected_site.id,
            start_at=period.start_at,
            end_at=period.end_at,
            probe_ids=filters.probe_ids,
            status_groups=filters.status_groups,
        )
        if selected_site is not None
        else 0
    )
    context = _build_dashboard_context(
            username=username,
            sites=sites,
            selected_site=selected_site,
            probes=probes,
            latest_results=latest_results,
            period_results=period_results,
            detail_results=detail_results,
            detail_limit=detail_limit,
            detail_page=detail_page,
            detail_count=detail_count,
            period=period,
            filters=filters,
            min_date=min_date,
            max_date=today,
        )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        context,
    )


def require_admin(request: Request) -> str:
    username = _current_admin_username(request)
    if username is None:
        raise PermissionError("Admin session is required")
    return username


def _current_admin_username(request: Request) -> str | None:
    if admin_auth_disabled():
        return LOCAL_ADMIN_USERNAME
    return verify_admin_session_token(request.cookies.get(ADMIN_SESSION_COOKIE))


def _ensure_development_seed(connection: sqlite3.Connection) -> None:
    if list_active_sites(connection):
        return
    seed_development_data(connection)


def _select_site(
    connection: sqlite3.Connection,
    *,
    sites: list[Site],
    site_id: int | None,
) -> Site | None:
    if site_id is not None:
        site = get_site(connection, site_id)
        if site is not None and site.enabled:
            return site
    return sites[0] if sites else None


def _bounded_date(value: date, *, min_date: date, max_date: date) -> date:
    return min(max(value, min_date), max_date)


def _parse_dashboard_period(
    *,
    date_value: str | None,
    from_time_value: str | None,
    to_time_value: str | None,
    min_date: date,
    max_date: date,
) -> DashboardPeriod:
    messages: list[str] = []
    try:
        selected_date = date.fromisoformat(date_value) if date_value else max_date
    except ValueError:
        selected_date = max_date
        messages.append("Некорректная дата заменена сегодняшней.")

    bounded_date = _bounded_date(selected_date, min_date=min_date, max_date=max_date)
    if bounded_date != selected_date:
        messages.append("Дата ограничена доступным периодом хранения.")
    selected_date = bounded_date

    selected_from = _parse_time_value(
        from_time_value,
        default=DEFAULT_FROM_TIME,
        field_name="From",
        messages=messages,
    )
    selected_to = _parse_time_value(
        to_time_value,
        default=DEFAULT_TO_TIME,
        field_name="To",
        messages=messages,
    )
    if selected_from > selected_to:
        selected_from = DEFAULT_FROM_TIME
        selected_to = DEFAULT_TO_TIME
        messages.append("From не может быть позже To; выбран полный день.")

    local_start = datetime.combine(selected_date, selected_from, tzinfo=MSK)
    local_end = datetime.combine(selected_date, selected_to, tzinfo=MSK) + timedelta(
        minutes=1
    )
    return DashboardPeriod(
        selected_date=selected_date,
        from_time=selected_from,
        to_time=selected_to,
        start_at=local_start.astimezone(UTC),
        end_at=local_end.astimezone(UTC),
        message=" ".join(messages) or None,
    )


def _parse_time_value(
    value: str | None,
    *,
    default: time,
    field_name: str,
    messages: list[str],
) -> time:
    if not value:
        return default
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        messages.append(f"Некорректное время {field_name} сброшено.")
        return default


def _parse_dashboard_filters(
    probe_value: str | None,
    status_value: str | None,
    *,
    probes: list[Probe],
) -> DashboardFilters:
    allowed_probes = tuple(probe.id for probe in probes)

    def selected(value: str | None, allowed: tuple[str, ...]) -> tuple[str, ...]:
        if value is None:
            return allowed
        requested = set(filter(None, value.split(",")))
        return tuple(item for item in allowed if item in requested)

    return DashboardFilters(
        probe_ids=selected(probe_value, allowed_probes),
        status_groups=selected(status_value, STATUS_GROUPS),
    )


def _render_login_page(*, error: str | None = None) -> str:
    return templates.get_template("login.html").render(error=error)


def _build_dashboard_context(
    *, username: str, sites: list[Site], selected_site: Site | None,
    probes: list[Probe], latest_results: dict[str, CheckResult],
    period_results: list[CheckResult], detail_results: list[CheckResult],
    detail_limit: int, detail_page: int, detail_count: int,
    period: DashboardPeriod, filters: DashboardFilters,
    min_date: date, max_date: date,
) -> dict[str, object]:
    return {
        "username": username,
        "sites_view": _build_sites_view(
            sites, selected_site, period, detail_limit, filters
        ),
        "summary_view": _build_probe_summary_view(
            probes,
            latest_results,
            period_results,
            period=period,
            now=datetime.now(UTC),
        ),
        "date_form": _build_date_form_view(
            selected_site,
            period,
            min_date,
            max_date,
            detail_limit,
            filters,
        ),
        "detail_controls": _build_detail_controls_view(
            selected_site, period, detail_limit, filters
        ),
        "chart": _build_response_time_chart_view(
            period_results, probes, period, filters
        ),
        "detail_rows": _build_result_rows(detail_results, probes),
        "pagination": _build_detail_pagination_view(
            selected_site, period, detail_limit, detail_page, detail_count,
            filters,
        ),
        "filters": {
            "probes": filters.probe_query,
            "statuses": filters.status_query,
        },
        "auto_refresh": period.selected_date == max_date,
    }


def _build_sites_view(
    sites: list[Site], selected_site: Site | None,
    period: DashboardPeriod, detail_limit: int, filters: DashboardFilters,
) -> list[dict[str, object]]:
    selected_id = selected_site.id if selected_site is not None else None
    return [
        {
            "name": site.name,
            "href": "/dashboard?" + urlencode({
                "site_id": site.id,
                "date": period.selected_date.isoformat(),
                "from_time": period.from_time.strftime("%H:%M"),
                "to_time": period.to_time.strftime("%H:%M"),
                "limit": detail_limit,
                "probes": filters.probe_query,
                "statuses": filters.status_query,
            }),
            "selected": site.id == selected_id,
        }
        for site in sites
    ]


def _summarize_probe_period(
    probes: list[Probe], results: list[CheckResult], *, period: DashboardPeriod,
) -> tuple[list[ProbePeriodSummary], float | None]:
    probe_ids = {probe.id for probe in probes}
    grouped: dict[str, list[CheckResult]] = defaultdict(list)
    for result in results:
        if result.probe_id in probe_ids:
            grouped[result.probe_id].append(result)

    expected_checks = max(
        (period.end_at - period.start_at).total_seconds() / PROBE_INTERVAL_SECONDS, 0
    )
    summaries = []
    total_received = 0
    total_successful = 0
    for probe in probes:
        probe_results = grouped[probe.id]
        received = len(probe_results)
        successful = sum(result.status_group == "2xx" for result in probe_results)
        response_times = [
            result.response_time_ms
            for result in probe_results
            if result.response_time_ms is not None
        ]
        counts = {
            status_group: sum(
                result.status_group == status_group for result in probe_results
            )
            for status_group in STATUS_GROUPS
        }
        summaries.append(ProbePeriodSummary(
            probe_id=probe.id,
            average_response_time_ms=(
                sum(response_times) / len(response_times) if response_times else None
            ),
            uptime_percent=(successful / received * 100 if received else None),
            received_checks=received,
            coverage_percent=(
                min(received / expected_checks * 100, 100) if expected_checks else 0
            ),
            status_counts=counts,
        ))
        total_received += received
        total_successful += successful
    overall = total_successful / total_received * 100 if total_received else None
    return summaries, overall


def _build_probe_summary_view(
    probes: list[Probe], latest_results: dict[str, CheckResult],
    period_results: list[CheckResult], *, period: DashboardPeriod, now: datetime,
    status_groups: tuple[str, ...] = STATUS_GROUPS,
) -> dict[str, object]:
    summaries, overall_uptime = _summarize_probe_period(
        probes, period_results, period=period
    )
    summaries_by_probe = {summary.probe_id: summary for summary in summaries}
    rows = []
    for probe in probes:
        result = latest_results.get(probe.id)
        summary = summaries_by_probe[probe.id]
        rows.append({
            "name": probe.name,
            "region": probe.region,
            "checked_at": _format_datetime(result.checked_at) if result else None,
            "stale": result is not None and now - result.checked_at > STALE_AFTER,
            "average_response": (
                f"{summary.average_response_time_ms:.1f} ms"
                if summary.average_response_time_ms is not None else "—"
            ),
            "uptime": (
                f"{summary.uptime_percent:.1f}%"
                if summary.uptime_percent is not None else "Нет данных"
            ),
            "received_checks": summary.received_checks,
            "coverage": f"{summary.coverage_percent:.1f}%",
            "status_counts": summary.status_counts,
        })
    return {
        "rows": rows,
        "status_groups": status_groups,
        "status_labels": {
            "2xx": "2xx", "3xx": "3xx", "4xx": "4xx", "5xx": "5xx",
            "network_error": "Сеть", "probe_error": "Probe",
        },
        "overall": (
            f"Общий uptime по полученным проверкам: {overall_uptime:.1f}%"
            if overall_uptime is not None
            else "Общий uptime: нет данных за выбранный период."
        ),
    }


def _render_probe_period_summary(
    probes: list[Probe], latest_results: dict[str, CheckResult],
    period_results: list[CheckResult], *, period: DashboardPeriod, now: datetime,
) -> str:
    return templates.get_template("partials/probe_summary.html").render(
        summary_view=_build_probe_summary_view(
            probes, latest_results, period_results, period=period, now=now
        )
    )


def _dashboard_query(
    selected_site: Site | None, period: DashboardPeriod,
    detail_limit: int, filters: DashboardFilters, **overrides: int | str,
) -> str:
    parameters: dict[str, int | str] = {
        "date": period.selected_date.isoformat(),
        "from_time": period.from_time.strftime("%H:%M"),
        "to_time": period.to_time.strftime("%H:%M"),
        "limit": detail_limit,
        "probes": filters.probe_query,
        "statuses": filters.status_query,
    }
    if selected_site is not None:
        parameters["site_id"] = selected_site.id
    parameters.update(overrides)
    return "/dashboard?" + urlencode(parameters)


def _date_navigation_action(
    *, label: str, aria_label: str, target_date: date, disabled: bool,
    selected_site: Site | None, period: DashboardPeriod, detail_limit: int,
    filters: DashboardFilters,
) -> dict[str, object]:
    return {
        "label": label, "aria_label": aria_label, "disabled": disabled,
        "href": None if disabled else _dashboard_query(
            selected_site, period, detail_limit, filters,
            date=target_date.isoformat(), page=1
        ),
    }


def _build_date_form_view(
    selected_site: Site | None, period: DashboardPeriod,
    min_date: date, max_date: date, detail_limit: int, filters: DashboardFilters,
) -> dict[str, object]:
    shared = {
        "selected_site": selected_site, "period": period,
        "detail_limit": detail_limit, "filters": filters,
    }
    return {
        "site_id": selected_site.id if selected_site else None,
        "message": period.message,
        "selected_date": period.selected_date.isoformat(),
        "min_date": min_date.isoformat(), "max_date": max_date.isoformat(),
        "from_time": period.from_time.strftime("%H:%M"),
        "to_time": period.to_time.strftime("%H:%M"),
        "detail_limit": detail_limit,
        "probes": filters.probe_query,
        "statuses": filters.status_query,
        "previous": _date_navigation_action(
            label="‹", aria_label="Предыдущий день",
            target_date=period.selected_date - timedelta(days=1),
            disabled=period.selected_date <= min_date, **shared
        ),
        "today": _date_navigation_action(
            label="Сегодня", aria_label="Перейти к сегодняшнему дню",
            target_date=max_date, disabled=period.selected_date == max_date, **shared
        ),
        "next": _date_navigation_action(
            label="›", aria_label="Следующий день",
            target_date=period.selected_date + timedelta(days=1),
            disabled=period.selected_date >= max_date, **shared
        ),
    }


def _build_detail_controls_view(
    selected_site: Site | None, period: DashboardPeriod, detail_limit: int,
    filters: DashboardFilters,
) -> dict[str, object]:
    return {
        "site_id": selected_site.id if selected_site else None,
        "date": period.selected_date.isoformat(),
        "from_time": period.from_time.strftime("%H:%M"),
        "to_time": period.to_time.strftime("%H:%M"),
        "detail_limit": detail_limit, "detail_limits": DETAIL_LIMITS,
        "probes": filters.probe_query, "statuses": filters.status_query,
    }


def _build_detail_pagination_view(
    selected_site: Site | None, period: DashboardPeriod, detail_limit: int,
    detail_page: int, detail_count: int, filters: DashboardFilters,
) -> dict[str, object]:
    total_pages = max(ceil(detail_count / detail_limit), 1)
    return {
        "page": detail_page, "total_pages": total_pages, "count": detail_count,
        "previous_href": (
            _dashboard_query(
                selected_site,
                period,
                detail_limit,
                filters,
                page=detail_page - 1,
            )
            if detail_page > 1 else None
        ),
        "next_href": (
            _dashboard_query(
                selected_site,
                period,
                detail_limit,
                filters,
                page=detail_page + 1,
            )
            if detail_page < total_pages else None
        ),
    }


def _build_response_time_chart_view(
    daily_results: list[CheckResult], probes: list[Probe], period: DashboardPeriod,
    filters: DashboardFilters | None = None,
) -> dict[str, object] | None:
    chartable = [item for item in daily_results if item.response_time_ms is not None]
    events = [
        item for item in daily_results
        if item.response_time_ms is None
        and item.status_group in ("network_error", "probe_error")
    ]
    if not chartable and not events:
        return None

    width, height, left, right, top, bottom = 900, 360, 56, 24, 20, 82
    plot_width, plot_height = width - left - right, height - top - bottom
    if filters is None:
        filters = DashboardFilters(
            probe_ids=tuple(probe.id for probe in probes),
            status_groups=STATUS_GROUPS,
        )
    visible_chartable = [
        item for item in chartable
        if item.probe_id in filters.probe_ids
        and item.status_group in filters.status_groups
    ]
    visible_max = max(
        (item.response_time_ms or 0 for item in visible_chartable), default=0
    )
    max_response = max(round(visible_max * 1.1), 10) if visible_max else 100
    names = {probe.id: probe.name for probe in probes}
    colors = _probe_colors(probes)
    grouped: dict[str, list[CheckResult]] = defaultdict(list)
    for result in daily_results:
        grouped[result.probe_id].append(result)
    period_seconds = max((period.end_at - period.start_at).total_seconds(), 60)

    def x_position(value: datetime) -> str:
        seconds = (value - period.start_at).total_seconds()
        return f"{left + seconds / period_seconds * plot_width:.1f}"

    def y_position(value: int) -> str:
        return f"{top + plot_height - value / max_response * plot_height:.1f}"

    grid_ticks = []
    for index in range(5):
        ratio = index / 4
        tick_at = period.start_at + timedelta(seconds=period_seconds * ratio)
        if index == 4:
            tick_at = period.end_at - timedelta(minutes=1)
        grid_ticks.append({
            "x": f"{left + ratio * plot_width:.1f}",
            "label": tick_at.astimezone(MSK).strftime("%H:%M MSK"),
        })
    y_ticks = [
        {
            "y": f"{top + plot_height - ratio * plot_height:.1f}",
            "label_y": f"{top + plot_height - ratio * plot_height + 4:.1f}",
            "label": f"{round(max_response * ratio)} ms",
        }
        for ratio in (0, 0.5, 1)
    ]

    series = []
    legends = []
    for probe in probes:
        probe_results = sorted(
            grouped.get(probe.id, []), key=lambda item: item.checked_at
        )
        color = colors[probe.id]
        segments = []
        for previous, current in zip(probe_results, probe_results[1:]):
            gap = (current.checked_at - previous.checked_at).total_seconds()
            if (
                previous.response_time_ms is None or current.response_time_ms is None
                or gap < 0 or gap > PROBE_INTERVAL_SECONDS * 2
            ):
                continue
            segments.append({
                "statuses": (
                    f"{_safe_status_css(previous.status_group)} "
                    f"{_safe_status_css(current.status_group)}"
                ),
                "color": color,
                "x1": x_position(previous.checked_at),
                "y1": y_position(previous.response_time_ms),
                "x2": x_position(current.checked_at),
                "y2": y_position(current.response_time_ms),
                "visible": (
                    probe.id in filters.probe_ids
                    and previous.status_group in filters.status_groups
                    and current.status_group in filters.status_groups
                ),
            })
        markers = []
        probe_events = []
        for result in probe_results:
            css_status = _safe_status_css(result.status_group)
            if result.response_time_ms is not None:
                title = (
                    f"{names.get(result.probe_id, result.probe_id)} "
                    f"{_format_datetime(result.checked_at)} "
                    f"{result.response_time_ms} ms {result.status_group} "
                    f"{result.http_status or result.error_type or ''}"
                )
                markers.append({
                    "class_name": (
                        "point-ok" if result.status_group == "2xx"
                        else f"problem-marker status-point-{css_status}"
                    ),
                    "status_group": css_status,
                    "cx": x_position(result.checked_at),
                    "cy": y_position(result.response_time_ms),
                    "response_time_ms": result.response_time_ms,
                    "visible": (
                        probe.id in filters.probe_ids
                        and result.status_group in filters.status_groups
                    ),
                    "title": title, "color": color,
                })
            elif result.status_group in ("network_error", "probe_error"):
                probe_events.append({
                    "class_name": f"problem-marker status-point-{css_status}",
                    "status_group": css_status,
                    "cx": x_position(result.checked_at),
                    "visible": (
                        probe.id in filters.probe_ids
                        and result.status_group in filters.status_groups
                    ),
                    "title": (
                        f"{names.get(result.probe_id, result.probe_id)} "
                        f"{_format_datetime(result.checked_at)} {result.status_group} "
                        f"{result.error_type or result.result_status}"
                    ),
                })
        series.append({
            "probe_id": probe.id, "segments": segments,
            "markers": markers, "events": probe_events,
        })
        legends.append({
            "probe_id": probe.id,
            "name": probe.name,
            "color": color,
            "enabled": probe.id in filters.probe_ids,
        })
    return {
        "width": width, "height": height, "left": left,
        "right": left + plot_width, "top": top, "bottom": top + plot_height,
        "event_y": top + plot_height + 32,
        "event_label_y": top + plot_height + 36,
        "event_label_x": left - 8,
        "grid_ticks": grid_ticks, "y_ticks": y_ticks,
        "series": series,
        "legends": legends,
        "status_groups": [
            {"name": group, "enabled": group in filters.status_groups}
            for group in STATUS_GROUPS
        ],
        "has_visible_response_data": bool(visible_chartable),
        "max_response": max_response,
    }


def _render_response_time_chart(
    daily_results: list[CheckResult], probes: list[Probe], period: DashboardPeriod,
    filters: DashboardFilters | None = None,
) -> str:
    return templates.get_template("partials/response_chart.html").render(
        chart=_build_response_time_chart_view(daily_results, probes, period, filters)
    )


def _build_result_rows(
    results: list[CheckResult], probes: list[Probe],
) -> list[dict[str, str]]:
    probe_names = {probe.id: probe.name for probe in probes}
    return [{
        "checked_at": _format_datetime(result.checked_at),
        "probe_name": probe_names.get(result.probe_id, result.probe_id),
        "status_group": result.status_group,
        "status_css": _safe_status_css(result.status_group),
        "detail": str(result.http_status or result.error_type or result.result_status),
        "response_time": (
            f"{result.response_time_ms} ms" if result.response_time_ms is not None else ""
        ),
    } for result in results]


def _parse_detail_limit(value: str | None) -> int:
    try:
        parsed = int(value) if value is not None else DEFAULT_DETAIL_LIMIT
    except (TypeError, ValueError):
        return DEFAULT_DETAIL_LIMIT
    return parsed if parsed in DETAIL_LIMITS else DEFAULT_DETAIL_LIMIT


def _parse_detail_page(value: str | None) -> int:
    try:
        parsed = int(value) if value is not None else 1
    except (TypeError, ValueError):
        return 1
    return parsed if parsed > 0 else 1


def _safe_status_css(status_group: str) -> str:
    return status_group if status_group in STATUS_GROUPS else "unknown"


def _probe_colors(probes: list[Probe]) -> dict[str, str]:
    colors: dict[str, str] = {}
    used_hues: set[int] = set()
    for probe in sorted(probes, key=lambda item: item.id):
        hue = int.from_bytes(sha256(probe.id.encode("utf-8")).digest()[:2], "big") % 360
        while hue in used_hues:
            hue = (hue + 47) % 360
        used_hues.add(hue)
        colors[probe.id] = f"hsl({hue} 68% 42%)"
    return colors


def _format_datetime(value: datetime) -> str:
    return f"{value.astimezone(MSK).strftime('%Y-%m-%d %H:%M:%S')} MSK"
