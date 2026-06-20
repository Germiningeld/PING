from __future__ import annotations

import sqlite3
from hashlib import sha256
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from html import escape
from typing import Annotated
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

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
    count_problem_results_for_site_in_period,
    get_site,
    list_active_sites,
    list_check_details_for_site_in_period,
    list_check_results_for_site_in_period,
    list_enabled_probes,
    list_latest_results_for_site_by_probe,
    list_problem_results_for_site_in_period,
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


@dataclass(frozen=True)
class DashboardPeriod:
    selected_date: date
    from_time: time
    to_time: time
    start_at: datetime
    end_at: datetime
    message: str | None = None


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
    return HTMLResponse(_render_login_page())


@router.post("/login")
async def login(request: Request) -> Response:
    if admin_auth_disabled():
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)

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
        )
        if selected_site is not None
        else []
    )
    recent_problems = (
        list_problem_results_for_site_in_period(
            connection,
            site_id=selected_site.id,
            start_at=period.start_at,
            end_at=period.end_at,
        )
        if selected_site is not None
        else []
    )
    problem_count = (
        count_problem_results_for_site_in_period(
            connection,
            site_id=selected_site.id,
            start_at=period.start_at,
            end_at=period.end_at,
        )
        if selected_site is not None
        else 0
    )

    return HTMLResponse(
        _render_dashboard_page(
            username=username,
            sites=sites,
            selected_site=selected_site,
            probes=probes,
            latest_results=latest_results,
            period_results=period_results,
            detail_results=detail_results,
            recent_problems=recent_problems,
            problem_count=problem_count,
            detail_limit=detail_limit,
            period=period,
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
        messages.append("Invalid date was reset to today.")

    bounded_date = _bounded_date(selected_date, min_date=min_date, max_date=max_date)
    if bounded_date != selected_date:
        messages.append("Date was limited to the available retention window.")
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
        messages.append("From must not be later than To; the full day was selected.")

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
        messages.append(f"Invalid {field_name} time was reset.")
        return default


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
    period_results: list[CheckResult],
    detail_results: list[CheckResult],
    recent_problems: list[CheckResult],
    problem_count: int,
    detail_limit: int,
    period: DashboardPeriod,
    min_date: date,
    max_date: date,
) -> str:
    sites_html = _render_sites(sites, selected_site, period, detail_limit)
    statuses_html = _render_probe_period_summary(
        probes,
        latest_results,
        period_results,
        period=period,
        now=datetime.now(UTC),
    )
    date_form_html = _render_date_form(
        selected_site, period, min_date, max_date, detail_limit
    )
    chart_html = _render_response_time_chart(period_results, probes)
    details_html = _render_daily_details(detail_results, probes)
    problems_html = _render_recent_problems(
        recent_problems, probes, total_count=problem_count
    )
    selected_name = selected_site.name if selected_site is not None else "No active site"
    selected_url = selected_site.url if selected_site is not None else ""
    auto_refresh_script = (
        "window.setTimeout(() => window.location.reload(), 60_000);"
        if period.selected_date == max_date
        else ""
    )

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
    .status-5xx {{ background: #ffccc7; color: #820014; }}
    .status-network_error {{ background: #5c0011; color: #fff1f0; }}
    .status-probe_error {{ background: #efdbff; color: #391085; }}
    .status-unknown {{ background: #f0f0f0; color: #595959; }}
    .stale {{ display: inline-block; padding: 4px 8px; border-radius: 999px; background: #fff1f0; color: #a8071a; font-weight: 700; }}
    .table-scroll {{ overflow-x: auto; }}
    .overall-uptime {{ margin-top: -18px; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: end; margin: 16px 0; }}
    .toolbar label {{ display: grid; gap: 4px; font-weight: 700; }}
    .toolbar input {{ padding: 8px; border: 1px solid #b8c2d6; border-radius: 6px; }}
    .toolbar button {{ padding: 9px 12px; border: 0; border-radius: 6px; background: #155eef; color: #fff; font-weight: 700; cursor: pointer; }}
    .date-navigation {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
    .date-nav-link {{ display: inline-block; padding: 8px 10px; border: 1px solid #b8c2d6; border-radius: 6px; background: #fff; font-weight: 700; }}
    .date-nav-link.disabled {{ color: #98a2b3; background: #f2f4f7; cursor: not-allowed; }}
    .chart-panel {{ background: #fff; border: 1px solid #d8deea; border-radius: 8px; padding: 16px; margin: 12px 0 24px; }}
    .chart {{ width: 100%; height: auto; min-height: 280px; overflow: visible; }}
    .axis {{ stroke: #b8c2d6; stroke-width: 1; }}
    .grid {{ stroke: #e6eaf2; stroke-width: 1; }}
    .series-segment {{ fill: none; stroke-width: 1.25; stroke-linecap: round; }}
    .point-hit {{ fill: transparent; stroke: none; pointer-events: all; }}
    .problem-marker {{ stroke: #fff; stroke-width: 1.5; pointer-events: all; }}
    .legend {{ display: flex; flex-wrap: wrap; gap: 10px 16px; margin: 12px 0; }}
    .legend-item, .status-toggle {{ display: inline-flex; gap: 6px; align-items: center; padding: 5px 8px; border: 1px solid #b8c2d6; border-radius: 999px; background: #fff; color: inherit; cursor: pointer; }}
    .legend-item[aria-pressed="false"] {{ opacity: 0.45; text-decoration: line-through; }}
    .swatch {{ width: 12px; height: 12px; border-radius: 999px; display: inline-block; }}
    .status-point-3xx {{ fill: #fa8c16; }}
    .status-point-4xx {{ fill: #f5222d; }}
    .status-point-5xx {{ fill: #a8071a; }}
    .status-point-network_error {{ fill: #5c0011; }}
    .status-point-probe_error {{ fill: #722ed1; }}
    .status-key {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0 16px; }}
    .status-filters {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0 16px; }}
    .event-strip-label {{ fill: #667085; font-size: 12px; }}
    .event-strip-line {{ stroke: #b8c2d6; stroke-width: 1; }}
    [hidden] {{ display: none !important; }}
    .muted {{ color: #667085; }}
    .error {{ padding: 10px; background: #fff1f0; border: 1px solid #ffccc7; color: #a8071a; border-radius: 6px; }}
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
      <h2>Probe Period Summary</h2>
      {statuses_html}
      <h2>Response Time History</h2>
      {date_form_html}
      {chart_html}
      <h2>Check Details</h2>
      {details_html}
      <h2>Problems for selected period</h2>
      {problems_html}
    </section>
  </main>
  <script>
    {auto_refresh_script}
    (() => {{
      const chart = document.querySelector("[data-response-chart]");
      if (!chart) return;
      const storageKey = "ping-dashboard-chart-filters-v1";
      let saved = {{ probes: {{}}, statuses: {{}} }};
      try {{ saved = JSON.parse(localStorage.getItem(storageKey)) || saved; }} catch (_) {{}}
      saved.probes ||= {{}};
      saved.statuses ||= {{}};

      const probeButtons = [...chart.querySelectorAll("[data-probe-toggle]")];
      const statusInputs = [...chart.querySelectorAll("[data-status-toggle]")];
      const enabled = (group, key) => saved[group][key] !== false;

      function applyFilters() {{
        probeButtons.forEach((button) => {{
          button.setAttribute("aria-pressed", String(enabled("probes", button.dataset.probeToggle)));
        }});
        statusInputs.forEach((input) => {{
          input.checked = enabled("statuses", input.dataset.statusToggle);
        }});
        chart.querySelectorAll("[data-filter-item]").forEach((item) => {{
          const probeVisible = enabled("probes", item.dataset.probeId);
          const statuses = (item.dataset.statuses || item.dataset.statusGroup || "").split(" ").filter(Boolean);
          const statusesVisible = statuses.every((value) => enabled("statuses", value));
          if (probeVisible && statusesVisible) {{
            item.removeAttribute("hidden");
          }} else {{
            item.setAttribute("hidden", "");
          }}
        }});
      }}

      function persist() {{
        try {{ localStorage.setItem(storageKey, JSON.stringify(saved)); }} catch (_) {{}}
      }}

      probeButtons.forEach((button) => button.addEventListener("click", () => {{
        const key = button.dataset.probeToggle;
        saved.probes[key] = !enabled("probes", key);
        persist();
        applyFilters();
      }}));
      statusInputs.forEach((input) => input.addEventListener("change", () => {{
        saved.statuses[input.dataset.statusToggle] = input.checked;
        persist();
        applyFilters();
      }}));
      applyFilters();
    }})();
  </script>
</body>
</html>"""


def _render_sites(
    sites: list[Site],
    selected_site: Site | None,
    period: DashboardPeriod,
    detail_limit: int,
) -> str:
    if not sites:
        return '<p class="muted">No active sites configured.</p>'

    items = []
    selected_id = selected_site.id if selected_site is not None else None
    for site in sites:
        css_class = "site-link selected" if site.id == selected_id else "site-link"
        query = urlencode(
            {
                "site_id": site.id,
                "date": period.selected_date.isoformat(),
                "from_time": period.from_time.strftime("%H:%M"),
                "to_time": period.to_time.strftime("%H:%M"),
                "limit": detail_limit,
            }
        )
        items.append(
            f'<li><a class="{css_class}" href="/dashboard?{escape(query, quote=True)}">'
            f"{escape(site.name)}</a></li>"
        )
    return f"<ul>{''.join(items)}</ul>"


def _summarize_probe_period(
    probes: list[Probe],
    results: list[CheckResult],
    *,
    period: DashboardPeriod,
) -> tuple[list[ProbePeriodSummary], float | None]:
    probe_ids = {probe.id for probe in probes}
    grouped: dict[str, list[CheckResult]] = defaultdict(list)
    for result in results:
        if result.probe_id in probe_ids:
            grouped[result.probe_id].append(result)

    expected_checks = max(
        (period.end_at - period.start_at).total_seconds() / PROBE_INTERVAL_SECONDS,
        0,
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
        summaries.append(
            ProbePeriodSummary(
                probe_id=probe.id,
                average_response_time_ms=(
                    sum(response_times) / len(response_times) if response_times else None
                ),
                uptime_percent=(successful / received * 100 if received else None),
                received_checks=received,
                coverage_percent=(
                    min(received / expected_checks * 100, 100)
                    if expected_checks
                    else 0
                ),
                status_counts=counts,
            )
        )
        total_received += received
        total_successful += successful

    overall_uptime = (
        total_successful / total_received * 100 if total_received else None
    )
    return summaries, overall_uptime


def _render_probe_period_summary(
    probes: list[Probe],
    latest_results: dict[str, CheckResult],
    period_results: list[CheckResult],
    *,
    period: DashboardPeriod,
    now: datetime,
) -> str:
    if not probes:
        return '<p class="muted">No enabled probes configured.</p>'

    summaries, overall_uptime = _summarize_probe_period(
        probes, period_results, period=period
    )
    summaries_by_probe = {summary.probe_id: summary for summary in summaries}
    rows = []
    for probe in probes:
        result = latest_results.get(probe.id)
        summary = summaries_by_probe[probe.id]
        if result is None:
            status_html = '<span class="status status-unknown">no data</span>'
            checked_at = "never"
            http_status = ""
        else:
            status_html = _status_badge(result.status_group)
            if now - result.checked_at > STALE_AFTER:
                status_html += ' <span class="stale">stale</span>'
            checked_at = _format_datetime(result.checked_at)
            http_status = str(result.http_status or result.error_type or "")

        average_response = (
            f"{summary.average_response_time_ms:.1f} ms"
            if summary.average_response_time_ms is not None
            else "—"
        )
        uptime = (
            f"{summary.uptime_percent:.1f}%"
            if summary.uptime_percent is not None
            else "No data"
        )

        rows.append(
            "<tr>"
            f"<td>{escape(probe.name)}</td>"
            f"<td>{escape(probe.region)}</td>"
            f"<td>{status_html}</td>"
            f"<td>{escape(http_status)}</td>"
            f"<td>{escape(checked_at)}</td>"
            f"<td>{escape(average_response)}</td>"
            f"<td>{escape(uptime)}</td>"
            f"<td>{summary.received_checks}</td>"
            f"<td>{summary.coverage_percent:.1f}%</td>"
            + "".join(
                f"<td>{summary.status_counts[status_group]}</td>"
                for status_group in STATUS_GROUPS
            )
            + "</tr>"
        )

    overall = (
        f"Overall uptime across received checks: {overall_uptime:.1f}%"
        if overall_uptime is not None
        else "Overall uptime: no data for the selected period."
    )
    status_headers = "".join(f"<th>{status_group}</th>" for status_group in STATUS_GROUPS)
    return (
        '<div class="table-scroll"><table><thead><tr><th>Probe</th><th>Region</th>'
        "<th>Last Status</th><th>Last HTTP/Error</th><th>Last Checked At</th>"
        "<th>Average Response Time</th><th>Uptime</th><th>Received</th>"
        f"<th>Coverage</th>{status_headers}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
        f'<p class="muted overall-uptime">{escape(overall)}</p>'
    )


def _render_date_form(
    selected_site: Site | None,
    period: DashboardPeriod,
    min_date: date,
    max_date: date,
    detail_limit: int,
) -> str:
    site_input = (
        f'<input type="hidden" name="site_id" value="{selected_site.id}">'
        if selected_site is not None
        else ""
    )
    message_html = (
        f'<p class="error">{escape(period.message)}</p>' if period.message else ""
    )
    limit_options = "".join(
        f'<option value="{value}"{" selected" if value == detail_limit else ""}>{value}</option>'
        for value in DETAIL_LIMITS
    )
    navigation_parameters = {
        "from_time": period.from_time.strftime("%H:%M"),
        "to_time": period.to_time.strftime("%H:%M"),
        "limit": detail_limit,
    }
    if selected_site is not None:
        navigation_parameters["site_id"] = selected_site.id

    def navigation_action(label: str, target_date: date, *, disabled: bool) -> str:
        if disabled:
            return (
                f'<span class="date-nav-link disabled" aria-disabled="true">'
                f"{escape(label)}</span>"
            )
        query = urlencode({**navigation_parameters, "date": target_date.isoformat()})
        return (
            f'<a class="date-nav-link" href="/dashboard?{escape(query, quote=True)}">'
            f"{escape(label)}</a>"
        )

    previous_action = navigation_action(
        "Previous day",
        period.selected_date - timedelta(days=1),
        disabled=period.selected_date <= min_date,
    )
    today_action = navigation_action(
        "Today",
        max_date,
        disabled=period.selected_date == max_date,
    )
    next_action = navigation_action(
        "Next day",
        period.selected_date + timedelta(days=1),
        disabled=period.selected_date >= max_date,
    )
    return f"""
      {message_html}
      <form class="toolbar" method="get" action="/dashboard">
        {site_input}
        <label for="date">Date
          <input id="date" name="date" type="date" value="{period.selected_date.isoformat()}"
            min="{min_date.isoformat()}" max="{max_date.isoformat()}">
        </label>
        <nav class="date-navigation" aria-label="Date navigation">
          {previous_action}
          {today_action}
          {next_action}
        </nav>
        <label for="from_time">From (MSK)
          <input id="from_time" name="from_time" type="time"
            value="{period.from_time.strftime('%H:%M')}" required>
        </label>
        <label for="to_time">To (MSK)
          <input id="to_time" name="to_time" type="time"
            value="{period.to_time.strftime('%H:%M')}" required>
        </label>
        <label for="limit">Check Details rows
          <select id="limit" name="limit">{limit_options}</select>
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
    event_results = [
        result
        for result in daily_results
        if result.response_time_ms is None
        and result.status_group in ("network_error", "probe_error")
    ]
    if not chartable_results and not event_results:
        return '<p class="muted">No response time data for the selected period.</p>'

    width = 900
    height = 360
    left = 56
    right = 24
    top = 20
    bottom = 82
    plot_width = width - left - right
    plot_height = height - top - bottom
    max_response_time = max(
        (result.response_time_ms or 0 for result in chartable_results), default=100
    )
    max_response_time = max(max_response_time, 100)
    probe_names = {probe.id: probe.name for probe in probes}
    probe_colors = _probe_colors(probes)
    grouped: dict[str, list[CheckResult]] = defaultdict(list)
    for result in daily_results:
        grouped[result.probe_id].append(result)

    def x_position(value: datetime) -> float:
        seconds = (
            value.astimezone(MSK).hour * 3600
            + value.astimezone(MSK).minute * 60
            + value.astimezone(MSK).second
        )
        return left + (seconds / 86399) * plot_width

    def y_position(response_time_ms: int) -> float:
        return top + plot_height - (response_time_ms / max_response_time) * plot_height

    grid_lines = []
    for hour in (0, 6, 12, 18, 24):
        x = left + (hour / 24) * plot_width
        label = (f"{hour:02d}:00" if hour < 24 else "24:00") + " MSK"
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
    for probe in probes:
        probe_id = probe.id
        results = grouped.get(probe_id, [])
        sorted_results = sorted(results, key=lambda result: result.checked_at)
        color = probe_colors[probe_id]
        segments = []
        for previous, current in zip(sorted_results, sorted_results[1:]):
            gap_seconds = (current.checked_at - previous.checked_at).total_seconds()
            if (
                previous.response_time_ms is None
                or current.response_time_ms is None
                or gap_seconds < 0
                or gap_seconds > PROBE_INTERVAL_SECONDS * 2
            ):
                continue
            statuses = f"{_safe_status_css(previous.status_group)} {_safe_status_css(current.status_group)}"
            segments.append(
                f'<line class="series-segment" data-filter-item data-probe-id="{escape(probe_id)}" '
                f'data-statuses="{escape(statuses)}" style="stroke:{color}" '
                f'x1="{x_position(previous.checked_at):.1f}" '
                f'y1="{y_position(previous.response_time_ms):.1f}" '
                f'x2="{x_position(current.checked_at):.1f}" '
                f'y2="{y_position(current.response_time_ms):.1f}"></line>'
            )

        markers = []
        for result in sorted_results:
            if result.response_time_ms is None:
                continue
            title = (
                f"{probe_names.get(result.probe_id, result.probe_id)} "
                f"{_format_datetime(result.checked_at)} "
                f"{result.response_time_ms} ms "
                f"{result.status_group} "
                f"{result.http_status or result.error_type or ''}"
            )
            css_status = _safe_status_css(result.status_group)
            marker_class = (
                "point-hit"
                if result.status_group == "2xx"
                else f"problem-marker status-point-{css_status}"
            )
            marker_radius = "7" if result.status_group == "2xx" else "4"
            markers.append(
                f'<circle class="{marker_class}" data-filter-item data-probe-id="{escape(probe_id)}" '
                f'data-status-group="{escape(css_status)}" '
                f'cx="{x_position(result.checked_at):.1f}" '
                f'cy="{y_position(result.response_time_ms):.1f}" r="{marker_radius}">'
                f"<title>{escape(title)}</title></circle>"
            )
        events = []
        for result in sorted_results:
            if (
                result.response_time_ms is not None
                or result.status_group not in ("network_error", "probe_error")
            ):
                continue
            css_status = _safe_status_css(result.status_group)
            title = (
                f"{probe_names.get(result.probe_id, result.probe_id)} "
                f"{_format_datetime(result.checked_at)} "
                f"{result.status_group} {result.error_type or result.result_status}"
            )
            events.append(
                f'<circle class="problem-marker status-point-{css_status}" data-filter-item '
                f'data-probe-id="{escape(probe_id)}" data-status-group="{escape(css_status)}" '
                f'cx="{x_position(result.checked_at):.1f}" cy="{top + plot_height + 32}" r="5">'
                f"<title>{escape(title)}</title></circle>"
            )
        series_html.append(
            f'<g data-probe-series="{escape(probe_id)}">'
            f"{''.join(segments)}{''.join(markers)}{''.join(events)}</g>"
        )
        legend_items.append(
            f'<button class="legend-item" type="button" data-probe-toggle="{escape(probe_id)}" aria-pressed="true">'
            f'<span class="swatch" style="background:{color}"></span>{escape(probe.name)}</button>'
        )

    status_toggles = "".join(
        f'<label class="status-toggle"><input type="checkbox" data-status-toggle="{status_group}" checked>'
        f'{_status_badge(status_group)}</label>'
        for status_group in STATUS_GROUPS
    )
    event_strip = (
        f'<line class="event-strip-line" x1="{left}" y1="{top + plot_height + 32}" '
        f'x2="{left + plot_width}" y2="{top + plot_height + 32}"></line>'
        f'<text class="event-strip-label" x="{left - 8}" y="{top + plot_height + 36}" '
        'text-anchor="end">Errors</text>'
    )

    return (
        '<div class="chart-panel" data-response-chart>'
        f'<div class="legend">{"".join(legend_items)}</div>'
        f'<div class="status-filters" aria-label="Status group filters">{status_toggles}</div>'
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        'aria-label="Response time by probe for selected period">'
        f'{"".join(grid_lines)}{"".join(y_labels)}'
        f'<line class="axis" x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}"></line>'
        f'<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}"></line>'
        f'{event_strip}{"".join(series_html)}'
        "</svg></div>"
    )


def _render_daily_details(
    daily_results: list[CheckResult],
    probes: list[Probe],
) -> str:
    if not daily_results:
        return '<p class="muted">No check results for the selected period.</p>'

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
        "<table><thead><tr><th>Timestamp (MSK)</th><th>Probe</th><th>Status</th>"
        "<th>HTTP/Error</th><th>Response Time</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_recent_problems(
    recent_problems: list[CheckResult],
    probes: list[Probe],
    *,
    total_count: int,
) -> str:
    count_html = f'<p class="muted">Total problems: {total_count}</p>'
    if not recent_problems:
        return (
            f'{count_html}<p class="muted">'
            "За выбранный период проблем не зафиксировано</p>"
        )

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

    return count_html + (
        "<table><thead><tr><th>Timestamp (MSK)</th><th>Probe</th><th>Status</th>"
        "<th>HTTP/Error</th><th>Response Time</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _parse_detail_limit(value: str | None) -> int:
    try:
        parsed = int(value) if value is not None else DEFAULT_DETAIL_LIMIT
    except (TypeError, ValueError):
        return DEFAULT_DETAIL_LIMIT
    return parsed if parsed in DETAIL_LIMITS else DEFAULT_DETAIL_LIMIT


def _status_badge(status_group: str) -> str:
    css_status = _safe_status_css(status_group)
    return (
        f'<span class="status status-{escape(css_status)}">'
        f"{escape(status_group)}</span>"
    )


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
