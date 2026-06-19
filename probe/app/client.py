from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from probe.app.config import ProbeRuntimeConfig
from probe.app.storage import SyncedProbeConfig, synced_config_from_payload


class ProbeCentralClient:
    def __init__(self, config: ProbeRuntimeConfig) -> None:
        self.config = config

    def fetch_config(self, *, timeout_seconds: int = 10) -> SyncedProbeConfig:
        request = Request(
            f"{self.config.central_api_url}/api/probe/config",
            headers=self._headers(),
            method="GET",
        )
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return synced_config_from_payload(payload)

    def submit_results(
        self,
        results: list[dict[str, Any]],
        *,
        timeout_seconds: int = 10,
    ) -> int:
        if not results:
            return 0

        request = Request(
            f"{self.config.central_api_url}/api/probe/results",
            data=json.dumps({"results": results}).encode("utf-8"),
            headers={**self._headers(), "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"Central API rejected probe results: {exc.code}") from exc

        accepted = int(payload.get("accepted", 0))
        if accepted != len(results):
            raise RuntimeError(
                f"Central API accepted {accepted} of {len(results)} probe results"
            )
        return accepted

    def _headers(self) -> dict[str, str]:
        return {
            "X-Probe-Id": self.config.probe_id,
            "Authorization": f"Bearer {self.config.probe_token}",
        }
