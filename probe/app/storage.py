from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SiteConfig:
    id: int
    name: str
    url: str


@dataclass(frozen=True)
class SyncedProbeConfig:
    probe_id: str
    sites: list[SiteConfig]
    check_interval_seconds: int
    timeout_seconds: int
    max_redirects: int


class ProbeStorage:
    def __init__(self, storage_dir: str | Path) -> None:
        self.storage_dir = Path(storage_dir)
        self.config_path = self.storage_dir / "sites-config.json"
        self.queue_path = self.storage_dir / "results-queue.json"

    def save_config(self, config: SyncedProbeConfig) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "probe_id": config.probe_id,
            "sites": [asdict(site) for site in config.sites],
            "check_interval_seconds": config.check_interval_seconds,
            "timeout_seconds": config.timeout_seconds,
            "max_redirects": config.max_redirects,
        }
        self.config_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def load_config(self) -> SyncedProbeConfig | None:
        if not self.config_path.exists():
            return None

        raw_config = json.loads(self.config_path.read_text(encoding="utf-8"))
        return synced_config_from_payload(raw_config)

    def load_queue(self) -> list[dict[str, Any]]:
        if not self.queue_path.exists():
            return []

        raw_queue = json.loads(self.queue_path.read_text(encoding="utf-8"))
        if not isinstance(raw_queue, list):
            raise ValueError("Probe result queue must be a JSON list")
        return raw_queue

    def append_queue(self, results: list[dict[str, Any]]) -> None:
        if not results:
            return

        self.storage_dir.mkdir(parents=True, exist_ok=True)
        queue = self.load_queue()
        queue.extend(results)
        self._write_queue(queue)

    def clear_queue(self) -> None:
        if self.queue_path.exists():
            self._write_queue([])

    def _write_queue(self, queue: list[dict[str, Any]]) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.queue_path.write_text(
            json.dumps(queue, indent=2, sort_keys=True),
            encoding="utf-8",
        )


def synced_config_from_payload(payload: dict[str, Any]) -> SyncedProbeConfig:
    sites = payload.get("sites", [])
    if not isinstance(sites, list):
        raise ValueError("Probe config sites must be a list")

    return SyncedProbeConfig(
        probe_id=str(payload["probe_id"]),
        sites=[
            SiteConfig(id=int(site["id"]), name=str(site["name"]), url=str(site["url"]))
            for site in sites
        ],
        check_interval_seconds=int(payload.get("check_interval_seconds", 60)),
        timeout_seconds=int(payload.get("timeout_seconds", 10)),
        max_redirects=int(payload.get("max_redirects", 0)),
    )
