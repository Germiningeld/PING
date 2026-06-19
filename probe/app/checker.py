from __future__ import annotations

import socket
import ssl
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from probe.app.storage import SiteConfig


BODY_READ_LIMIT_BYTES = 64 * 1024


@dataclass(frozen=True)
class ProbeCheckResult:
    site_id: int
    checked_at: datetime
    result_status: str
    status_group: str
    http_status: int | None = None
    response_time_ms: int | None = None
    error_type: str | None = None
    error_message: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "checked_at": self.checked_at.isoformat(),
            "result_status": self.result_status,
            "status_group": self.status_group,
            "http_status": self.http_status,
            "response_time_ms": self.response_time_ms,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def classify_http_status(http_status: int) -> tuple[str, str]:
    if 200 <= http_status <= 299:
        return "ok", "2xx"
    if 300 <= http_status <= 399:
        return "redirect_problem", "3xx"
    if 400 <= http_status <= 499:
        return "client_error", "4xx"
    if 500 <= http_status <= 599:
        return "server_error", "5xx"
    return "probe_error", "probe_error"


def check_site(
    site: SiteConfig,
    *,
    timeout_seconds: int = 10,
    body_read_limit_bytes: int = BODY_READ_LIMIT_BYTES,
) -> ProbeCheckResult:
    checked_at = datetime.now(UTC).replace(microsecond=0)
    started_at = time.monotonic()
    request = Request(site.url, method="GET", headers={"User-Agent": "PING-probe/0.1"})
    opener = build_opener(NoRedirectHandler)

    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            response.read(body_read_limit_bytes)
            response_time_ms = _elapsed_ms(started_at)
            result_status, status_group = classify_http_status(response.status)
            return ProbeCheckResult(
                site_id=site.id,
                checked_at=checked_at,
                result_status=result_status,
                status_group=status_group,
                http_status=response.status,
                response_time_ms=response_time_ms,
            )
    except HTTPError as exc:
        exc.read(body_read_limit_bytes)
        response_time_ms = _elapsed_ms(started_at)
        result_status, status_group = classify_http_status(exc.code)
        return ProbeCheckResult(
            site_id=site.id,
            checked_at=checked_at,
            result_status=result_status,
            status_group=status_group,
            http_status=exc.code,
            response_time_ms=response_time_ms,
        )
    except (URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
        return ProbeCheckResult(
            site_id=site.id,
            checked_at=checked_at,
            result_status="network_error",
            status_group="network_error",
            error_type=exc.__class__.__name__,
            error_message=str(exc)[:1024],
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeCheckResult(
            site_id=site.id,
            checked_at=checked_at,
            result_status="probe_error",
            status_group="probe_error",
            error_type=exc.__class__.__name__,
            error_message=str(exc)[:1024],
        )


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))
