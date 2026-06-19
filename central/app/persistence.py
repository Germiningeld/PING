from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, time
from pathlib import Path

from central.app.auth import hash_probe_token
from central.app.models import CheckResult, Probe, Site


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS probes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    region TEXT NOT NULL,
    probe_type TEXT NOT NULL,
    network_label TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS check_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL,
    probe_id TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    result_status TEXT NOT NULL,
    status_group TEXT NOT NULL,
    http_status INTEGER,
    response_time_ms INTEGER,
    error_type TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (site_id) REFERENCES sites(id),
    FOREIGN KEY (probe_id) REFERENCES probes(id)
);

CREATE INDEX IF NOT EXISTS idx_check_results_site_id
    ON check_results(site_id);
CREATE INDEX IF NOT EXISTS idx_check_results_probe_id
    ON check_results(probe_id);
CREATE INDEX IF NOT EXISTS idx_check_results_checked_at
    ON check_results(checked_at);
CREATE INDEX IF NOT EXISTS idx_check_results_site_id_checked_at
    ON check_results(site_id, checked_at);
"""


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def connect_database(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    if db_path != Path(":memory:"):
        db_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    connection.commit()


def create_site(
    connection: sqlite3.Connection,
    *,
    name: str,
    url: str,
    enabled: bool = True,
) -> Site:
    now = utc_now()
    cursor = connection.execute(
        """
        INSERT INTO sites (name, url, enabled, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (name, url, int(enabled), _to_db_datetime(now), _to_db_datetime(now)),
    )
    connection.commit()
    site = get_site(connection, int(cursor.lastrowid))
    if site is None:
        raise RuntimeError("Created site was not found")
    return site


def create_probe(
    connection: sqlite3.Connection,
    *,
    probe_id: str,
    name: str,
    region: str,
    token_hash: str,
    probe_type: str = "datacenter",
    network_label: str = "datacenter",
    enabled: bool = True,
) -> Probe:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO probes (
            id, name, region, probe_type, network_label, enabled, token_hash,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            probe_id,
            name,
            region,
            probe_type,
            network_label,
            int(enabled),
            token_hash,
            _to_db_datetime(now),
            _to_db_datetime(now),
        ),
    )
    connection.commit()
    probe = get_probe(connection, probe_id)
    if probe is None:
        raise RuntimeError("Created probe was not found")
    return probe


def create_check_result(
    connection: sqlite3.Connection,
    *,
    site_id: int,
    probe_id: str,
    checked_at: datetime,
    result_status: str,
    status_group: str,
    http_status: int | None = None,
    response_time_ms: int | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> CheckResult:
    now = utc_now()
    cursor = connection.execute(
        """
        INSERT INTO check_results (
            site_id, probe_id, checked_at, result_status, status_group,
            http_status, response_time_ms, error_type, error_message, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            site_id,
            probe_id,
            _to_db_datetime(checked_at),
            result_status,
            status_group,
            http_status,
            response_time_ms,
            error_type,
            error_message,
            _to_db_datetime(now),
        ),
    )
    connection.commit()
    result = get_check_result(connection, int(cursor.lastrowid))
    if result is None:
        raise RuntimeError("Created check result was not found")
    return result


def get_site(connection: sqlite3.Connection, site_id: int) -> Site | None:
    row = connection.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
    return _row_to_site(row) if row is not None else None


def get_probe(connection: sqlite3.Connection, probe_id: str) -> Probe | None:
    row = connection.execute("SELECT * FROM probes WHERE id = ?", (probe_id,)).fetchone()
    return _row_to_probe(row) if row is not None else None


def get_check_result(
    connection: sqlite3.Connection,
    check_result_id: int,
) -> CheckResult | None:
    row = connection.execute(
        "SELECT * FROM check_results WHERE id = ?",
        (check_result_id,),
    ).fetchone()
    return _row_to_check_result(row) if row is not None else None


def list_active_sites(connection: sqlite3.Connection) -> list[Site]:
    rows = connection.execute(
        "SELECT * FROM sites WHERE enabled = 1 ORDER BY id"
    ).fetchall()
    return [_row_to_site(row) for row in rows]


def list_enabled_probes(connection: sqlite3.Connection) -> list[Probe]:
    rows = connection.execute(
        "SELECT * FROM probes WHERE enabled = 1 ORDER BY id"
    ).fetchall()
    return [_row_to_probe(row) for row in rows]


def list_check_results_for_site(
    connection: sqlite3.Connection,
    *,
    site_id: int,
) -> list[CheckResult]:
    rows = connection.execute(
        """
        SELECT * FROM check_results
        WHERE site_id = ?
        ORDER BY checked_at, id
        """,
        (site_id,),
    ).fetchall()
    return [_row_to_check_result(row) for row in rows]


def list_check_results_for_site_on_date(
    connection: sqlite3.Connection,
    *,
    site_id: int,
    selected_date: date,
) -> list[CheckResult]:
    start_at = datetime.combine(selected_date, time.min, tzinfo=UTC)
    end_at = datetime.combine(selected_date, time.max, tzinfo=UTC)
    rows = connection.execute(
        """
        SELECT *
        FROM check_results
        WHERE site_id = ?
            AND checked_at >= ?
            AND checked_at <= ?
        ORDER BY checked_at, probe_id, id
        """,
        (site_id, _to_db_datetime(start_at), _to_db_datetime(end_at)),
    ).fetchall()
    return [_row_to_check_result(row) for row in rows]


def list_check_results_for_site_in_period(
    connection: sqlite3.Connection,
    *,
    site_id: int,
    start_at: datetime,
    end_at: datetime,
) -> list[CheckResult]:
    """Return results in the half-open UTC interval [start_at, end_at)."""
    rows = connection.execute(
        """
        SELECT *
        FROM check_results
        WHERE site_id = ?
            AND checked_at >= ?
            AND checked_at < ?
        ORDER BY checked_at, probe_id, id
        """,
        (site_id, _to_db_datetime(start_at), _to_db_datetime(end_at)),
    ).fetchall()
    return [_row_to_check_result(row) for row in rows]


def list_check_details_for_site_in_period(
    connection: sqlite3.Connection,
    *,
    site_id: int,
    start_at: datetime,
    end_at: datetime,
    limit: int,
) -> list[CheckResult]:
    """Return newest results for the details table in a half-open UTC interval."""
    rows = connection.execute(
        """
        SELECT *
        FROM check_results
        WHERE site_id = ?
            AND checked_at >= ?
            AND checked_at < ?
        ORDER BY checked_at DESC, probe_id, id DESC
        LIMIT ?
        """,
        (
            site_id,
            _to_db_datetime(start_at),
            _to_db_datetime(end_at),
            limit,
        ),
    ).fetchall()
    return [_row_to_check_result(row) for row in rows]


def list_latest_results_for_site_by_probe(
    connection: sqlite3.Connection,
    *,
    site_id: int,
) -> dict[str, CheckResult]:
    rows = connection.execute(
        """
        SELECT cr.*
        FROM check_results cr
        INNER JOIN (
            SELECT probe_id, MAX(checked_at) AS latest_checked_at
            FROM check_results
            WHERE site_id = ?
            GROUP BY probe_id
        ) latest
            ON latest.probe_id = cr.probe_id
            AND latest.latest_checked_at = cr.checked_at
        WHERE cr.site_id = ?
        ORDER BY cr.probe_id, cr.id DESC
        """,
        (site_id, site_id),
    ).fetchall()

    latest_results: dict[str, CheckResult] = {}
    for row in rows:
        result = _row_to_check_result(row)
        latest_results.setdefault(result.probe_id, result)
    return latest_results


def list_recent_problem_results(
    connection: sqlite3.Connection,
    *,
    site_id: int,
    selected_date: date | None = None,
    limit: int = 10,
) -> list[CheckResult]:
    date_filter = ""
    parameters: list[object] = [site_id]
    if selected_date is not None:
        start_at = datetime.combine(selected_date, time.min, tzinfo=UTC)
        end_at = datetime.combine(selected_date, time.max, tzinfo=UTC)
        date_filter = "AND checked_at >= ? AND checked_at <= ?"
        parameters.extend([_to_db_datetime(start_at), _to_db_datetime(end_at)])
    parameters.append(limit)

    rows = connection.execute(
        f"""
        SELECT *
        FROM check_results
        WHERE site_id = ?
            AND status_group != '2xx'
            {date_filter}
        ORDER BY checked_at DESC, id DESC
        LIMIT ?
        """,
        parameters,
    ).fetchall()
    return [_row_to_check_result(row) for row in rows]


def list_problem_results_for_site_in_period(
    connection: sqlite3.Connection,
    *,
    site_id: int,
    start_at: datetime,
    end_at: datetime,
    limit: int = 10,
) -> list[CheckResult]:
    """Return newest non-2xx results in the half-open UTC interval."""
    rows = connection.execute(
        """
        SELECT *
        FROM check_results
        WHERE site_id = ?
            AND status_group != '2xx'
            AND checked_at >= ?
            AND checked_at < ?
        ORDER BY checked_at DESC, id DESC
        LIMIT ?
        """,
        (
            site_id,
            _to_db_datetime(start_at),
            _to_db_datetime(end_at),
            limit,
        ),
    ).fetchall()
    return [_row_to_check_result(row) for row in rows]


def count_problem_results_for_site_in_period(
    connection: sqlite3.Connection,
    *,
    site_id: int,
    start_at: datetime,
    end_at: datetime,
) -> int:
    """Count non-2xx results in the half-open UTC interval."""
    row = connection.execute(
        """
        SELECT COUNT(*) AS problem_count
        FROM check_results
        WHERE site_id = ?
            AND status_group != '2xx'
            AND checked_at >= ?
            AND checked_at < ?
        """,
        (
            site_id,
            _to_db_datetime(start_at),
            _to_db_datetime(end_at),
        ),
    ).fetchone()
    return int(row["problem_count"])


def cleanup_check_results_older_than(
    connection: sqlite3.Connection,
    *,
    cutoff: datetime,
) -> int:
    cursor = connection.execute(
        "DELETE FROM check_results WHERE checked_at < ?",
        (_to_db_datetime(cutoff),),
    )
    connection.commit()
    return cursor.rowcount


def seed_development_data(connection: sqlite3.Connection) -> None:
    now = _to_db_datetime(utc_now())
    connection.execute(
        """
        INSERT OR IGNORE INTO sites (name, url, enabled, created_at, updated_at)
        VALUES (?, ?, 1, ?, ?)
        """,
        ("Example Site", "https://example.com/", now, now),
    )

    for probe_id, name, region in (
        ("ru-dc-1", "Russia Datacenter", "Russia"),
        ("eu-dc-1", "Europe Datacenter", "Europe"),
        ("us-dc-1", "United States Datacenter", "United States"),
    ):
        connection.execute(
            """
            INSERT OR IGNORE INTO probes (
                id, name, region, probe_type, network_label, enabled,
                token_hash, created_at, updated_at
            )
            VALUES (?, ?, ?, 'datacenter', 'datacenter', 1, ?, ?, ?)
            """,
            (
                probe_id,
                name,
                region,
                hash_probe_token(f"dev-token-{probe_id}"),
                now,
                now,
            ),
        )

    connection.commit()


def _to_db_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _from_db_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _row_to_site(row: sqlite3.Row) -> Site:
    return Site(
        id=int(row["id"]),
        name=str(row["name"]),
        url=str(row["url"]),
        enabled=bool(row["enabled"]),
        created_at=_from_db_datetime(str(row["created_at"])),
        updated_at=_from_db_datetime(str(row["updated_at"])),
    )


def _row_to_probe(row: sqlite3.Row) -> Probe:
    return Probe(
        id=str(row["id"]),
        name=str(row["name"]),
        region=str(row["region"]),
        probe_type=str(row["probe_type"]),
        network_label=str(row["network_label"]),
        enabled=bool(row["enabled"]),
        token_hash=str(row["token_hash"]),
        created_at=_from_db_datetime(str(row["created_at"])),
        updated_at=_from_db_datetime(str(row["updated_at"])),
    )


def _row_to_check_result(row: sqlite3.Row) -> CheckResult:
    return CheckResult(
        id=int(row["id"]),
        site_id=int(row["site_id"]),
        probe_id=str(row["probe_id"]),
        checked_at=_from_db_datetime(str(row["checked_at"])),
        result_status=str(row["result_status"]),
        status_group=str(row["status_group"]),
        http_status=row["http_status"],
        response_time_ms=row["response_time_ms"],
        error_type=row["error_type"],
        error_message=row["error_message"],
        created_at=_from_db_datetime(str(row["created_at"])),
    )
