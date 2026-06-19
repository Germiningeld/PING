from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Site:
    id: int
    name: str
    url: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Probe:
    id: str
    name: str
    region: str
    probe_type: str
    network_label: str
    enabled: bool
    token_hash: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CheckResult:
    id: int
    site_id: int
    probe_id: str
    checked_at: datetime
    result_status: str
    status_group: str
    http_status: int | None
    response_time_ms: int | None
    error_type: str | None
    error_message: str | None
    created_at: datetime
