from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.error import URLError

from probe.app.agent import ProbeAgent
from probe.app.checker import (
    NoRedirectHandler,
    ProbeCheckResult,
    check_site,
    classify_http_status,
)
from probe.app.storage import ProbeStorage, SiteConfig, SyncedProbeConfig


def test_classify_http_status_groups() -> None:
    assert classify_http_status(200) == ("ok", "2xx")
    assert classify_http_status(204) == ("ok", "2xx")
    assert classify_http_status(301) == ("redirect_problem", "3xx")
    assert classify_http_status(404) == ("client_error", "4xx")
    assert classify_http_status(500) == ("server_error", "5xx")
    assert classify_http_status(99) == ("probe_error", "probe_error")


def test_no_redirect_handler_disables_redirects() -> None:
    handler = NoRedirectHandler()

    redirect_request = handler.redirect_request(
        None,
        None,
        302,
        "Found",
        {},
        "https://example.com/target",
    )

    assert redirect_request is None


def test_check_site_reads_body_with_limit(monkeypatch) -> None:
    response = FakeHttpResponse(status=200)
    opener = FakeOpener(response=response)
    monkeypatch.setattr("probe.app.checker.build_opener", lambda handler: opener)

    result = check_site(
        SiteConfig(id=1, name="Example", url="https://example.com/"),
        body_read_limit_bytes=17,
    )

    assert result.result_status == "ok"
    assert result.status_group == "2xx"
    assert result.http_status == 200
    assert response.read_limit == 17


def test_check_site_classifies_network_errors(monkeypatch) -> None:
    opener = FakeOpener(error=URLError("connection refused"))
    monkeypatch.setattr("probe.app.checker.build_opener", lambda handler: opener)

    result = check_site(SiteConfig(id=1, name="Example", url="https://example.com/"))

    assert result.result_status == "network_error"
    assert result.status_group == "network_error"
    assert result.error_type == "URLError"


def test_storage_saves_config_and_queue(tmp_path) -> None:
    storage = ProbeStorage(tmp_path)
    config = SyncedProbeConfig(
        probe_id="ru-dc-1",
        sites=[SiteConfig(id=1, name="Example", url="https://example.com/")],
        check_interval_seconds=60,
        timeout_seconds=10,
        max_redirects=0,
    )

    storage.save_config(config)
    storage.append_queue([{"site_id": 1, "result_status": "ok"}])

    assert storage.load_config() == config
    assert storage.load_queue() == [{"site_id": 1, "result_status": "ok"}]


def test_agent_saves_synced_config_and_submits_results(tmp_path, monkeypatch) -> None:
    config = SyncedProbeConfig(
        probe_id="ru-dc-1",
        sites=[SiteConfig(id=1, name="Example", url="https://example.com/")],
        check_interval_seconds=60,
        timeout_seconds=10,
        max_redirects=0,
    )
    client = FakeCentralClient(config=config)
    storage = ProbeStorage(tmp_path)
    checked_at = datetime(2026, 6, 19, 9, 30, tzinfo=UTC)

    def fake_check_site(site, *, timeout_seconds):  # noqa: ANN001
        return ProbeCheckResult(
            site_id=site.id,
            checked_at=checked_at,
            result_status="ok",
            status_group="2xx",
            http_status=200,
            response_time_ms=123,
        )

    monkeypatch.setattr("probe.app.agent.check_site", fake_check_site)

    summary = ProbeAgent(client=client, storage=storage).run_once()

    assert summary.sites_checked == 1
    assert summary.submitted_results == 1
    assert summary.queued_results == 0
    assert storage.load_config() == config
    assert client.submitted_results == [
        {
            "site_id": 1,
            "checked_at": "2026-06-19T09:30:00+00:00",
            "result_status": "ok",
            "status_group": "2xx",
            "http_status": 200,
            "response_time_ms": 123,
            "error_type": None,
            "error_message": None,
        }
    ]


def test_agent_queues_results_when_submission_fails(tmp_path, monkeypatch) -> None:
    config = SyncedProbeConfig(
        probe_id="ru-dc-1",
        sites=[SiteConfig(id=1, name="Example", url="https://example.com/")],
        check_interval_seconds=60,
        timeout_seconds=10,
        max_redirects=0,
    )
    client = FakeCentralClient(config=config, submit_fails=True)
    storage = ProbeStorage(tmp_path)

    def fake_check_site(site, *, timeout_seconds):  # noqa: ANN001
        return ProbeCheckResult(
            site_id=site.id,
            checked_at=datetime(2026, 6, 19, 9, 30, tzinfo=UTC),
            result_status="network_error",
            status_group="network_error",
            error_type="URLError",
            error_message="connection failed",
        )

    monkeypatch.setattr("probe.app.agent.check_site", fake_check_site)

    summary = ProbeAgent(client=client, storage=storage).run_once()

    queued = storage.load_queue()
    assert summary.sites_checked == 1
    assert summary.submitted_results == 0
    assert summary.queued_results == 1
    assert queued[0]["result_status"] == "network_error"


def test_agent_flushes_existing_queue_before_new_results(tmp_path, monkeypatch) -> None:
    config = SyncedProbeConfig(
        probe_id="ru-dc-1",
        sites=[SiteConfig(id=1, name="Example", url="https://example.com/")],
        check_interval_seconds=60,
        timeout_seconds=10,
        max_redirects=0,
    )
    client = FakeCentralClient(config=config)
    storage = ProbeStorage(tmp_path)
    queued_result = {"site_id": 1, "checked_at": "2026-06-19T09:29:00+00:00"}
    storage.append_queue([queued_result])

    def fake_check_site(site, *, timeout_seconds):  # noqa: ANN001
        return ProbeCheckResult(
            site_id=site.id,
            checked_at=datetime(2026, 6, 19, 9, 30, tzinfo=UTC),
            result_status="ok",
            status_group="2xx",
            http_status=200,
        )

    monkeypatch.setattr("probe.app.agent.check_site", fake_check_site)

    summary = ProbeAgent(client=client, storage=storage).run_once()

    assert summary.submitted_results == 2
    assert summary.queued_results == 0
    assert storage.load_queue() == []
    assert client.submitted_batches[0] == [queued_result]


def test_agent_uses_cached_config_when_sync_fails(tmp_path, monkeypatch) -> None:
    cached_config = SyncedProbeConfig(
        probe_id="ru-dc-1",
        sites=[SiteConfig(id=1, name="Cached", url="https://cached.example/")],
        check_interval_seconds=60,
        timeout_seconds=10,
        max_redirects=0,
    )
    client = FakeCentralClient(config=cached_config, config_fails=True)
    storage = ProbeStorage(tmp_path)
    storage.save_config(cached_config)

    def fake_check_site(site, *, timeout_seconds):  # noqa: ANN001
        return ProbeCheckResult(
            site_id=site.id,
            checked_at=datetime(2026, 6, 19, 9, 30, tzinfo=UTC),
            result_status="ok",
            status_group="2xx",
            http_status=200,
        )

    monkeypatch.setattr("probe.app.agent.check_site", fake_check_site)

    summary = ProbeAgent(client=client, storage=storage).run_once()

    assert summary.sites_checked == 1
    assert summary.submitted_results == 1


def test_queue_file_is_plain_json(tmp_path) -> None:
    storage = ProbeStorage(tmp_path)
    storage.append_queue([{"site_id": 1}])

    raw_queue = json.loads(storage.queue_path.read_text(encoding="utf-8"))

    assert raw_queue == [{"site_id": 1}]


class FakeCentralClient:
    def __init__(
        self,
        *,
        config: SyncedProbeConfig,
        config_fails: bool = False,
        submit_fails: bool = False,
    ) -> None:
        self.config = config
        self.config_fails = config_fails
        self.submit_fails = submit_fails
        self.submitted_results = []
        self.submitted_batches = []

    def fetch_config(self):
        if self.config_fails:
            raise RuntimeError("config unavailable")
        return self.config

    def submit_results(self, results, *, timeout_seconds):  # noqa: ANN001
        if self.submit_fails:
            raise RuntimeError("central unavailable")
        self.submitted_batches.append(results)
        self.submitted_results.extend(results)
        return len(results)


class FakeOpener:
    def __init__(self, *, response=None, error=None) -> None:  # noqa: ANN001
        self.response = response
        self.error = error

    def open(self, request, timeout):  # noqa: ANN001
        if self.error is not None:
            raise self.error
        return self.response


class FakeHttpResponse:
    def __init__(self, *, status: int) -> None:
        self.status = status
        self.read_limit = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001
        return False

    def read(self, limit: int) -> bytes:
        self.read_limit = limit
        return b"ok"
