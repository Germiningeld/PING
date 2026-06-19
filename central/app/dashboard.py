from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from html import escape
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from central.app.auth import (
    ADMIN_SESSION_COOKIE,
    ADMIN_SESSION_MAX_AGE_SECONDS,
    admin_auth_configured,
    cookie_secure_enabled,
    create_admin_session_token,
    verify_admin_credentials,
    verify_admin_session_token,
)
from central.app.models import CheckResult, Probe, Site
from central.app.persistence import (
    get_site,
    list_active_sites,
    list_check_results_for_site_on_date,
    list_enabled_probes,
    list_latest_results_for_site_by_probe,
    list_recent_problem_results,
    seed_development_data,
)
from central.app.probe_api import DatabaseConnection


router = APIRouter(tags=["dashboard"])
RETENTION_DAYS = 90
STATUS_GROUPS = ("2xx", "3xx", "4xx", "5xx", "network_error", "probe_error")


@router.get("/", response_class=HTMLResponse)
def root(request: Request) -> Response:
    if _current_admin_username(request) is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/login", response_class=HTMLResponse)
def login_screen(request: Request) -> Response:
    if _current_admin_username(request) is not None:
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return HTMLResponse(_render_login_page())


@router.post("/login")
async def login(request: Request) -> Response:
    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    username = form.get("username", [""])[0]
    password = form.get("password", [""])[0]

    if not admin_auth_configured():
        return HTMLResponse(
            _render_login_page(error="Admin credentials are not configured."),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    if not verify_admin_credentials(username=username, password=password):
        return HTMLResponse(
            _render_login_page(error="Invalid username or password."),
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
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
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
    selected_date: Annotated[date | None, Query(alias="date")] = None,
) -> Response:
    username = _current_admin_username(request)
    if username is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    _ensure_development_seed(connection)
    sites = list_active_sites(connection)
    selected_site = _select_site(connection, sites=sites, site_id=site_id)
    probes = list_enabled_probes(connection)
    today = datetime.now(UTC).date()
    min_date = today - timedelta(days=RETENTION_DAYS - 1)
    chart_date = _bounded_date(selected_date or today, min_date=min_date, max_date=today)
    latest_results = (
        list_latest_results_for_site_by_probe(connection, site_id=selected_site.id)
        if selected_site is not None
        else {}
    )
    daily_results = (
        list_check_results_for_site_on_date(
            connection,
            site_id=selected_site.id,
            selected_date=chart_date,
        )
        if selected_site is not None
        else []
    )
    recent_problems = (
        list_recent_problem_results(
            connection,
            site_id=selected_site.id,
            selected_date=chart_date,
        )
        if selected_site is not None
        else []
    )

    return HTMLResponse(
        _render_dashboard_page(
            username=username,
            sites=sites,
            selected_site=selected_site,
            probes=probes,
            latest_results=latest_results,
            daily_results=daily_results,
            recent_problems=recent_problems,
            selected_date=chart_date,
            min_date=min_date,
            max_date=today,
        )
    )


def require_admin(request: Request) -> str:
    username = _current_admin_username(request)
    if username is None:
        raise PermissionError("Admin session is required")
    return username


def _current_admin_username(request: Request) -> str | None:
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


def _render_login_page(*, error: str | None = None) -> str:
    error_html = f'<p class="error">{escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PING Admin Login</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 0; background: #f5f7fb; color: #18202f; }}
    main {{ max-width: 360px; margin: 12vh auto; padding: 24px; background: #fff; border: 1px solid #d8deea; border-radius: 8px; }}
    label {{ display: block; margin: 14px 0 6px; font-weight: 700; }}
    input {{ box-sizing: border-box; width: 100%; padding: 10px; border: 1px solid #b8c2d6; border-radius: 6px; }}
    button {{ margin-top: 18px; width: 100%; padding: 10px 12px; border: 0; border-radius: 6px; background: #155eef; color: #fff; font-weight: 700; cursor: pointer; }}
    .error {{ padding: 10px; background: #fff1f0; border: 1px solid #ffccc7; color: #a8071a; border-radius: 6px; }}
  </style>
</head>
<body>
  <main>
    <h1>PING Admin</h1>
    {error_html}
    <form method="post" action="/login">
      <label for="username">Username</label>
      <input id="username" name="username" autocomplete="username" required>
      <label for="password">Password</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required>
      <button type="submit">Sign in</button>
    </form>
  </main>
</body>
</html>"""


def _render_dashboard_page(
    *,
    username: str,
    sites: list[Site],
    selected_site: Site | None,
    probes: list[Probe],
    latest_results: dict[str, CheckResult],
    daily_results: list[CheckResult],
    recent_problems: list[CheckResult],
    selected_date: date,
    min_date: date,
    max_date: date,
) -> str:
    sites_html = _render_sites(sites, selected_site)
    statuses_html = _render_statuses(probes, latest_results)
    date_form_html = _render_date_form(selected_site, selected_date, min_date, max_date)
    chart_html = _render_response_time_chart(daily_results, probes)
    details_html = _render_daily_details(daily_results, probes)
    problems_html = _render_recent_problems(recent_problems, probes)
    selected_name = selected_site.name if selected_site is not None else "No active site"
    selected_url = selected_site.url if selected_site is not None else ""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PING Dashboard</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 0; background: #f5f7fb; color: #18202f; }}
    header {{ display: flex; justify-content: space-between; align-items: center; padding: 16px 24px; background: #fff; border-bottom: 1px solid #d8deea; }}
    main {{ display: grid; grid-template-columns: 280px 1fr; min-height: calc(100vh - 66px); }}
    aside {{ padding: 20px; border-right: 1px solid #d8deea; background: #fff; }}
    section {{ padding: 24px; }}
    a {{ color: #155eef; text-decoration: none; }}
    ul {{ list-style: none; padding: 0; margin: 0; }}
    li {{ margin-bottom: 8px; }}
    .site-link {{ display: block; padding: 10px; border-radius: 6px; border: 1px solid transparent; }}
    .selected {{ border-color: #155eef; background: #edf3ff; font-weight: 700; }}
    table {{ width: 100%; border-collapse: collapse; margin: 16px 0 28px; background: #fff; }}
    th, td {{ padding: 10px; border: 1px solid #d8deea; text-align: left; vertical-align: top; }}
    th {{ background: #edf1f7; }}
    .status {{ display: inline-block; min-width: 96px; padding: 4px 8px; border-radius: 999px; text-align: center; font-weight: 700; }}
    .status-2xx {{ background: #d9f7be; color: #135200; }}
    .status-3xx {{ background: #ffe7ba; color: #ad4e00; }}
    .status-4xx {{ background: #ffd8bf; color: #ad2102; }}
    .status-5xx, .status-network_error {{ background: #ffa39e; color: #820014; }}
    .status-probe_error {{ background: #efdbff; color: #391085; }}
    .status-unknown {{ background: #f0f0f0; color: #595959; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: end; margin: 16px 0; }}
    .toolbar label {{ display: grid; gap: 4px; font-weight: 700; }}
    .toolbar input {{ padding: 8px; border: 1px solid #b8c2d6; border-radius: 6px; }}
    .toolbar button {{ padding: 9px 12px; border: 0; border-radius: 6px; background: #155eef; color: #fff; font-weight: 700; cursor: pointer; }}
    .chart-panel {{ background: #fff; border: 1px solid #d8deea; border-radius: 8px; padding: 16px; margin: 12px 0 24px; }}
    .chart {{ width: 100%; height: auto; min-height: 280px; overflow: visible; }}
    .axis {{ stroke: #b8c2d6; stroke-width: 1; }}
    .grid {{ stroke: #e6eaf2; stroke-width: 1; }}
    .series {{ fill: none; stroke-width: 2.5; }}
    .point {{ stroke: #fff; stroke-width: 1.5; }}
    .legend {{ display: flex; flex-wrap: wrap; gap: 10px 16px; margin: 12px 0; }}
    .legend-item {{ display: inline-flex; gap: 6px; align-items: center; }}
    .swatch {{ width: 12px; height: 12px; border-radius: 999px; display: inline-block; }}
    .probe-ru-dc-1, .swatch-ru-dc-1 {{ stroke: #155eef; background: #155eef; }}
    .probe-eu-dc-1, .swatch-eu-dc-1 {{ stroke: #13a8a8; background: #13a8a8; }}
    .probe-us-dc-1, .swatch-us-dc-1 {{ stroke: #722ed1; background: #722ed1; }}
    .probe-default, .swatch-default {{ stroke: #475467; background: #475467; }}
    .point.status-point-2xx {{ fill: #52c41a; }}
    .point.status-point-3xx {{ fill: #fa8c16; }}
    .point.status-point-4xx {{ fill: #f5222d; }}
    .point.status-point-5xx {{ fill: #a8071a; }}
    .point.status-point-network_error {{ fill: #5c0011; }}
    .point.status-point-probe_error {{ fill: #722ed1; }}
    .status-key {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0 16px; }}
    .muted {{ color: #667085; }}
    .logout {{ background: none; border: 1px solid #b8c2d6; border-radius: 6px; padding: 8px 10px; cursor: pointer; }}
  </style>
</head>
<body>
  <header>
    <div><strong>PING Dashboard</strong> <span class="muted">signed in as {escape(username)}</span></div>
    <form method="post" action="/logout"><button class="logout" type="submit">Logout</button></form>
  </header>
  <main>
    <aside>
      <h2>Sites</h2>
      {sites_html}
    </aside>
    <section>
      <h1>{escape(selected_name)}</h1>
      <p class="muted">{escape(selected_url)}</p>
      <h2>Current Probe Status</h2>
      {statuses_html}
      <h2>Response Time History</h2>
      {date_form_html}
      {chart_html}
      <h2>Check Details</h2>
      {details_html}
      <h2>Recent Problems</h2>
      {problems_html}
    </section>
  </main>
</body>
</html>"""


def _render_sites(sites: list[Site], selected_site: Site | None) -> str:
    if not sites:
        return '<p class="muted">No active sites configured.</p>'

    items = []
    selected_id = selected_site.id if selected_site is not None else None
    for site in sites:
        css_class = "site-link selected" if site.id == selected_id else "site-link"
        items.append(
            f'<li><a class="{css_class}" href="/dashboard?site_id={site.id}">'
            f"{escape(site.name)}</a></li>"
        )
    return f"<ul>{''.join(items)}</ul>"


def _render_statuses(
    probes: list[Probe],
    latest_results: dict[str, CheckResult],
) -> str:
    if not probes:
        return '<p class="muted">No enabled probes configured.</p>'

    rows = []
    for probe in probes:
        result = latest_results.get(probe.id)
        if result is None:
            status_html = '<span class="status status-unknown">no data</span>'
            checked_at = "never"
            http_status = ""
            response_time = ""
        else:
            status_html = _status_badge(result.status_group)
            checked_at = _format_datetime(result.checked_at)
            http_status = str(result.http_status or result.error_type or "")
            response_time = (
                f"{result.response_time_ms} ms"
                if result.response_time_ms is not None
                else ""
            )

        rows.append(
            "<tr>"
            f"<td>{escape(probe.name)}</td>"
            f"<td>{escape(probe.region)}</td>"
            f"<td>{status_html}</td>"
            f"<td>{escape(http_status)}</td>"
            f"<td>{escape(response_time)}</td>"
            f"<td>{escape(checked_at)}</td>"
            "</tr>"
        )

    return (
        "<table><thead><tr><th>Probe</th><th>Region</th><th>Status</th>"
        "<th>HTTP/Error</th><th>Response Time</th><th>Checked At</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_date_form(
    selected_site: Site | None,
    selected_date: date,
    min_date: date,
    max_date: date,
) -> str:
    site_input = (
        f'<input type="hidden" name="site_id" value="{selected_site.id}">'
        if selected_site is not None
        else ""
    )
    return f"""
      <form class="toolbar" method="get" action="/dashboard">
        {site_input}
        <label for="date">Date
          <input id="date" name="date" type="date" value="{selected_date.isoformat()}"
            min="{min_date.isoformat()}" max="{max_date.isoformat()}">
        </label>
        <button type="submit">Show</button>
      </form>
      <div class="status-key">
        {_status_badge("2xx")}
        {_status_badge("3xx")}
        {_status_badge("4xx")}
        {_status_badge("5xx")}
        {_status_badge("network_error")}
        {_status_badge("probe_error")}
      </div>
    """


def _render_response_time_chart(
    daily_results: list[CheckResult],
    probes: list[Probe],
) -> str:
    chartable_results = [
        result for result in daily_results if result.response_time_ms is not None
    ]
    if not chartable_results:
        return '<p class="muted">No response time data for the selected date.</p>'

    width = 900
    height = 320
    left = 56
    right = 24
    top = 20
    bottom = 42
    plot_width = width - left - right
    plot_height = height - top - bottom
    max_response_time = max(result.response_time_ms or 0 for result in chartable_results)
    max_response_time = max(max_response_time, 100)
    probe_names = {probe.id: probe.name for probe in probes}
    grouped: dict[str, list[CheckResult]] = defaultdict(list)
    for result in chartable_results:
        grouped[result.probe_id].append(result)

    def x_position(value: datetime) -> float:
        seconds = (
            value.astimezone(UTC).hour * 3600
            + value.astimezone(UTC).minute * 60
            + value.astimezone(UTC).second
        )
        return left + (seconds / 86399) * plot_width

    def y_position(response_time_ms: int) -> float:
        return top + plot_height - (response_time_ms / max_response_time) * plot_height

    grid_lines = []
    for hour in (0, 6, 12, 18, 24):
        x = left + (hour / 24) * plot_width
        label = f"{hour:02d}:00" if hour < 24 else "24:00"
        grid_lines.append(
            f'<line class="grid" x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_height}"></line>'
            f'<text x="{x:.1f}" y="{height - 12}" text-anchor="middle" fill="#667085" font-size="12">{label}</text>'
        )

    y_labels = []
    for ratio in (0, 0.5, 1):
        y = top + plot_height - ratio * plot_height
        value = round(max_response_time * ratio)
        y_labels.append(
            f'<line class="grid" x1="{left}" y1="{y:.1f}" x2="{left + plot_width}" y2="{y:.1f}"></line>'
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#667085" font-size="12">{value} ms</text>'
        )

    series_html = []
    legend_items = []
    for probe_id, results in grouped.items():
        css_probe = _probe_css_class(probe_id)
        sorted_results = sorted(results, key=lambda result: result.checked_at)
        points = " ".join(
            f"{x_position(result.checked_at):.1f},{y_position(result.response_time_ms or 0):.1f}"
            for result in sorted_results
        )
        circles = []
        for result in sorted_results:
            title = (
                f"{probe_names.get(result.probe_id, result.probe_id)} "
                f"{_format_datetime(result.checked_at)} "
                f"{result.response_time_ms} ms "
                f"{result.status_group} "
                f"{result.http_status or result.error_type or ''}"
            )
            circles.append(
                f'<circle class="point status-point-{_safe_status_css(result.status_group)}" '
                f'cx="{x_position(result.checked_at):.1f}" '
                f'cy="{y_position(result.response_time_ms or 0):.1f}" r="4">'
                f"<title>{escape(title)}</title></circle>"
            )
        series_html.append(
            f'<polyline class="series {css_probe}" points="{points}"></polyline>'
            f"{''.join(circles)}"
        )
        legend_items.append(
            f'<span class="legend-item"><span class="swatch swatch-{escape(css_probe.removeprefix("probe-"))}"></span>'
            f"{escape(probe_names.get(probe_id, probe_id))}</span>"
        )

    return (
        '<div class="chart-panel">'
        f'<div class="legend">{"".join(legend_items)}</div>'
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        'aria-label="Response time by probe for selected date">'
        f'{"".join(grid_lines)}{"".join(y_labels)}'
        f'<line class="axis" x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}"></line>'
        f'<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}"></line>'
        f'{"".join(series_html)}'
        "</svg></div>"
    )


def _render_daily_details(
    daily_results: list[CheckResult],
    probes: list[Probe],
) -> str:
    if not daily_results:
        return '<p class="muted">No check results for the selected date.</p>'

    probe_names = {probe.id: probe.name for probe in probes}
    rows = []
    for result in daily_results:
        detail = str(result.http_status or result.error_type or result.result_status)
        response_time = (
            f"{result.response_time_ms} ms"
            if result.response_time_ms is not None
            else ""
        )
        rows.append(
            "<tr>"
            f"<td>{escape(_format_datetime(result.checked_at))}</td>"
            f"<td>{escape(probe_names.get(result.probe_id, result.probe_id))}</td>"
            f"<td>{_status_badge(result.status_group)}</td>"
            f"<td>{escape(detail)}</td>"
            f"<td>{escape(response_time)}</td>"
            "</tr>"
        )

    return (
        "<table><thead><tr><th>Timestamp</th><th>Probe</th><th>Status</th>"
        "<th>HTTP/Error</th><th>Response Time</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_recent_problems(
    recent_problems: list[CheckResult],
    probes: list[Probe],
) -> str:
    if not recent_problems:
        return '<p class="muted">No recent problems for the selected site.</p>'

    probe_names = {probe.id: probe.name for probe in probes}
    rows = []
    for result in recent_problems:
        detail = str(result.http_status or result.error_type or result.result_status)
        response_time = (
            f"{result.response_time_ms} ms"
            if result.response_time_ms is not None
            else ""
        )
        rows.append(
            "<tr>"
            f"<td>{escape(_format_datetime(result.checked_at))}</td>"
            f"<td>{escape(probe_names.get(result.probe_id, result.probe_id))}</td>"
            f"<td>{_status_badge(result.status_group)}</td>"
            f"<td>{escape(detail)}</td>"
            f"<td>{escape(response_time)}</td>"
            "</tr>"
        )

    return (
        "<table><thead><tr><th>Timestamp</th><th>Probe</th><th>Status</th>"
        "<th>HTTP/Error</th><th>Response Time</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _status_badge(status_group: str) -> str:
    css_status = _safe_status_css(status_group)
    return (
        f'<span class="status status-{escape(css_status)}">'
        f"{escape(status_group)}</span>"
    )


def _safe_status_css(status_group: str) -> str:
    return status_group if status_group in STATUS_GROUPS else "unknown"


def _probe_css_class(probe_id: str) -> str:
    known_probe_classes = {
        "ru-dc-1": "probe-ru-dc-1",
        "eu-dc-1": "probe-eu-dc-1",
        "us-dc-1": "probe-us-dc-1",
    }
    return known_probe_classes.get(probe_id, "probe-default")


def _format_datetime(value: datetime) -> str:
    return value.isoformat(timespec="seconds")
