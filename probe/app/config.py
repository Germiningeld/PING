from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProbeRuntimeConfig:
    probe_id: str
    probe_token: str
    central_api_url: str
    storage_dir: Path


def load_runtime_config(path: str | Path) -> ProbeRuntimeConfig:
    config_path = Path(path)
    raw_config = json.loads(config_path.read_text(encoding="utf-8"))

    return ProbeRuntimeConfig(
        probe_id=_read_required_string(raw_config, "probe_id"),
        probe_token=_read_required_string(raw_config, "probe_token"),
        central_api_url=_read_required_string(raw_config, "central_api_url").rstrip("/"),
        storage_dir=Path(raw_config.get("storage_dir", "data/probe")),
    )


def _read_required_string(raw_config: dict[str, Any], key: str) -> str:
    value = raw_config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing required probe config value: {key}")
    return value.strip()
