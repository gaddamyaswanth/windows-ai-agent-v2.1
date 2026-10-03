"""
config.py — Absolute path robust configuration loader.
"""

from __future__ import annotations
from pathlib import Path
import yaml


class ConfigSection:
    def __init__(self, data: dict):
        for key, value in data.items():
            if isinstance(value, dict):
                setattr(self, key, ConfigSection(value))
            else:
                setattr(self, key, value)


class Config(ConfigSection):
    pass


def load_config(path: str = "config.yaml") -> Config:
    # Anchor to the directory where config.py lives (the project root)
    root_dir = Path(__file__).resolve().parent
    resolved_path = root_dir / path

    if not resolved_path.exists():
        resolved_path = Path(path).resolve()
        if not resolved_path.exists():
            raise FileNotFoundError(
                f"Config file not found at '{resolved_path}'. Make sure 'config.yaml' is in: {root_dir}"
            )

    with open(resolved_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    return Config(data)