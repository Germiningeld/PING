from __future__ import annotations

from dataclasses import dataclass

from probe.app.checker import check_site
from probe.app.client import ProbeCentralClient
from probe.app.storage import ProbeStorage, SyncedProbeConfig


@dataclass(frozen=True)
class ProbeRunSummary:
    config_synced: bool
    sites_checked: int
    submitted_results: int
    queued_results: int


class ProbeAgent:
    def __init__(self, client: ProbeCentralClient, storage: ProbeStorage) -> None:
        self.client = client
        self.storage = storage

    def run_once(self) -> ProbeRunSummary:
        config, config_synced = self._sync_or_load_config()
        submitted_results = self._flush_queue(config)

        results = [
            check_site(site, timeout_seconds=config.timeout_seconds).to_payload()
            for site in config.sites
        ]

        try:
            submitted_results += self.client.submit_results(
                results,
                timeout_seconds=config.timeout_seconds,
            )
        except Exception:  # noqa: BLE001
            self.storage.append_queue(results)

        return ProbeRunSummary(
            config_synced=config_synced,
            sites_checked=len(results),
            submitted_results=submitted_results,
            queued_results=len(self.storage.load_queue()),
        )

    def _sync_or_load_config(self) -> tuple[SyncedProbeConfig, bool]:
        try:
            config = self.client.fetch_config()
            self.storage.save_config(config)
            return config, True
        except Exception:  # noqa: BLE001
            cached_config = self.storage.load_config()
            if cached_config is None:
                raise RuntimeError("Probe config sync failed and no local cache exists")
            return cached_config, False

    def _flush_queue(self, config: SyncedProbeConfig) -> int:
        queued_results = self.storage.load_queue()
        if not queued_results:
            return 0

        try:
            accepted = self.client.submit_results(
                queued_results,
                timeout_seconds=config.timeout_seconds,
            )
        except Exception:  # noqa: BLE001
            return 0

        self.storage.clear_queue()
        return accepted
