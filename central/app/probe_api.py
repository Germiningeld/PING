from __future__ import annotations

import os
import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from central.app.auth import verify_probe_token
from central.app.persistence import (
    cleanup_check_results_older_than,
    connect_database,
    create_check_result,
    get_probe,
    get_site,
    initialize_database,
    list_active_sites,
)


router = APIRouter(prefix="/api/probe", tags=["probe"])
bearer_scheme = HTTPBearer(auto_error=False)
DEFAULT_RETENTION_DAYS = 90


class SiteConfigResponse(BaseModel):
    id: int
    name: str
    url: str


class ProbeConfigResponse(BaseModel):
    probe_id: str
    sites: list[SiteConfigResponse]
    check_interval_seconds: int
    timeout_seconds: int
    max_redirects: int


class CheckResultPayload(BaseModel):
    site_id: int = Field(gt=0)
    checked_at: datetime
    result_status: Literal[
        "ok",
        "redirect_problem",
        "client_error",
        "server_error",
        "network_error",
        "probe_error",
    ]
    status_group: Literal["2xx", "3xx", "4xx", "5xx", "network_error", "probe_error"]
    http_status: int | None = Field(default=None, ge=100, le=599)
    response_time_ms: int | None = Field(default=None, ge=0)
    error_type: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=1024)


class SubmitResultsRequest(BaseModel):
    results: list[CheckResultPayload] = Field(min_length=1, max_length=1000)


class SubmitResultsResponse(BaseModel):
    accepted: int


def get_database_connection() -> Generator[sqlite3.Connection, None, None]:
    database_path = os.getenv("PING_DATABASE_PATH", "data/dev-check.sqlite3")
    connection = connect_database(database_path)
    initialize_database(connection)
    try:
        yield connection
    finally:
        connection.close()


DatabaseConnection = Annotated[sqlite3.Connection, Depends(get_database_connection)]
ProbeIdHeader = Annotated[str, Header(alias="X-Probe-Id", min_length=1)]
ProbeCredentials = Annotated[
    HTTPAuthorizationCredentials | None,
    Depends(bearer_scheme),
]


def authenticate_probe(
    probe_id: ProbeIdHeader,
    credentials: ProbeCredentials,
    connection: DatabaseConnection,
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing probe bearer token",
        )

    probe = get_probe(connection, probe_id)
    if probe is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown probe",
        )

    if not verify_probe_token(token=credentials.credentials, token_hash=probe.token_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid probe token",
        )

    if not probe.enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Probe is disabled",
        )

    return probe.id


AuthenticatedProbeId = Annotated[str, Depends(authenticate_probe)]


@router.get("/config", response_model=ProbeConfigResponse)
def get_probe_config(
    probe_id: AuthenticatedProbeId,
    connection: DatabaseConnection,
) -> ProbeConfigResponse:
    sites = [
        SiteConfigResponse(id=site.id, name=site.name, url=site.url)
        for site in list_active_sites(connection)
    ]

    return ProbeConfigResponse(
        probe_id=probe_id,
        sites=sites,
        check_interval_seconds=60,
        timeout_seconds=10,
        max_redirects=0,
    )


@router.post("/results", response_model=SubmitResultsResponse)
def submit_probe_results(
    payload: SubmitResultsRequest,
    probe_id: AuthenticatedProbeId,
    connection: DatabaseConnection,
) -> SubmitResultsResponse:
    for result in payload.results:
        site = get_site(connection, result.site_id)
        if site is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown site_id: {result.site_id}",
            )
        if not site.enabled:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Disabled site_id: {result.site_id}",
            )

    for result in payload.results:
        create_check_result(
            connection,
            site_id=result.site_id,
            probe_id=probe_id,
            checked_at=result.checked_at,
            result_status=result.result_status,
            status_group=result.status_group,
            http_status=result.http_status,
            response_time_ms=result.response_time_ms,
            error_type=result.error_type,
            error_message=result.error_message,
        )

    cleanup_check_results_older_than(
        connection,
        cutoff=datetime.now(UTC) - timedelta(days=_retention_days()),
    )

    return SubmitResultsResponse(accepted=len(payload.results))


def _retention_days() -> int:
    raw_value = os.getenv("PING_RETENTION_DAYS")
    if raw_value is None:
        return DEFAULT_RETENTION_DAYS
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    return value if value > 0 else DEFAULT_RETENTION_DAYS
