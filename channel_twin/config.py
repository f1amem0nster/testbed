"""Configuration loading for a channel-twin node."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class TwinConfigError(ValueError):
    """The node configuration is missing or internally inconsistent."""


def _require(config: dict[str, Any], name: str, expected: type) -> Any:
    value = config.get(name)
    if not isinstance(value, expected):
        raise TwinConfigError(f"{name!r} must be {expected.__name__}")
    if expected is str and not value.strip():
        raise TwinConfigError(f"{name!r} cannot be empty")
    return value


def load_twin_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path).expanduser().resolve()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TwinConfigError(f"Configuration file does not exist: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise TwinConfigError(f"Invalid JSON in {config_path}: {exc}") from exc
    if not isinstance(config, dict):
        raise TwinConfigError("Configuration must be a JSON object")

    _require(config, "node_id", str)
    role = _require(config, "role", str)
    if role not in {"ground", "satellite"}:
        raise TwinConfigError("role must be 'ground' or 'satellite'")
    for name in ("peer_node_id", "api_listen_host", "data_listen_host", "peer_data_host", "spool_dir"):
        _require(config, name, str)
    for name in ("api_listen_port", "data_listen_port", "peer_data_port"):
        port = _require(config, name, int)
        if not 0 <= port <= 65535:
            raise TwinConfigError(f"{name} must be between 0 and 65535")
    _require(config, "auth_token", str)

    config.setdefault("max_artifact_bytes", 1_073_741_824)
    config.setdefault("attempt_timeout_seconds", 30.0)
    config.setdefault("retry_interval_seconds", 2.0)
    config.setdefault("io_chunk_bytes", 262_144)
    config.setdefault("scenario_id", "spaceverse-compatible-static")
    for name in ("max_artifact_bytes", "io_chunk_bytes"):
        if not isinstance(config[name], int) or config[name] <= 0:
            raise TwinConfigError(f"{name} must be a positive integer")
    for name in ("attempt_timeout_seconds", "retry_interval_seconds"):
        if not isinstance(config[name], (int, float)) or config[name] <= 0:
            raise TwinConfigError(f"{name} must be positive")

    config["config_path"] = str(config_path)
    config["spool_dir"] = str((config_path.parent / config["spool_dir"]).resolve()) \
        if not Path(config["spool_dir"]).is_absolute() else str(Path(config["spool_dir"]).resolve())
    if "scenario_file" in config:
        if not isinstance(config["scenario_file"], str) or not config["scenario_file"].strip():
            raise TwinConfigError("scenario_file must be a non-empty string")
        scenario_path = Path(config["scenario_file"])
        config["scenario_file"] = str(
            (config_path.parent / scenario_path).resolve() if not scenario_path.is_absolute() else scenario_path.resolve()
        )
    return config
