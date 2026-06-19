from __future__ import annotations

import sqlite3
from datetime import datetime
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
    list_enabled_probes,
    list_latest_results_for_site_by_probe,
    list_recent_problem_results,
    seed_development_data,
)
from central.app.probe_api import DatabaseConnection


router = APIRouter(tags=["dashboard"])


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
) -> Response:
    username = _current_admin_username(request)
    if username is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    _ensure_development_seed(connection)
    sites = list_active_sites(connection)
    selected_site = _select_site(connection, sites=sites, site_id=site_id)
    probes = list_enabled_probes(connection)
    latest_results = (
        list_latest_results_for_site_by_probe(connection, site_id=selected_site.id)
        if selected_site is not None
        else {}
    )
    recent_problems = (
        list_recent_problem_results(connection, site_id=selected_site.id)
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
            recent_problems=recent_problems,
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
    recent_problems: list[CheckResult],
) -> str:
    sites_html = _render_sites(sites, selected_site)
    statuses_html = _render_statuses(probes, latest_results)
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
    css_status = status_group if status_group in {
        "2xx",
        "3xx",
        "4xx",
        "5xx",
        "network_error",
        "probe_error",
    } else "unknown"
    return (
        f'<span class="status status-{escape(css_status)}">'
        f"{escape(status_group)}</span>"
    )


def _format_datetime(value: datetime) -> str:
    return value.isoformat(timespec="seconds")
