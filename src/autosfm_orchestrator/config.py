from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise ConfigError(f"Config file does not exist: {config_path}")

    with config_path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file) or {}

    _validate_minimal_config(config)
    return config


def _validate_minimal_config(config: dict[str, Any]) -> None:
    required_sections = ["workspace", "database", "globus", "paths", "autosfm"]
    missing = [section for section in required_sections if section not in config]
    if missing:
        raise ConfigError(f"Missing required config sections: {missing}")

    required_paths = ["ceres_source_root", "ceres_output_root", "sunny_staging_root"]
    missing_paths = [key for key in required_paths if key not in config["paths"]]
    if missing_paths:
        raise ConfigError(f"Missing required paths config values: {missing_paths}")
