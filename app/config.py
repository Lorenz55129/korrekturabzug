"""Load the YAML configuration – re-reads on every call (no stale cache)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: str | None = None) -> dict[str, Any]:
    """Read and return the config dict.  Re-reads the file every time so that
    changes to config.yaml take effect without a server restart."""
    config_path = Path(path) if path else Path(
        os.environ.get("KORREKTUR_CONFIG", str(_DEFAULT_CONFIG_PATH))
    )
    with open(config_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def get_field_mapping() -> dict[str, str]:
    return load_config().get("field_mapping", {})
