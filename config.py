"""
agent/config.py — Strict-but-forgiving configuration wrapper.

Reads config/config.yaml and exposes sections as attribute access:
    cfg.voice.sample_rate      -> int
    cfg.model.temperature      -> float

Missing keys that have known safe defaults are returned with a warning
(so a single omitted YAML key can't crash the main loop). Anything else
raises AttributeError listing available keys.

main.py imports `Config` from this module:
    from agent.config import Config, load_config
"""

import logging
import os

import yaml

logger = logging.getLogger("Config")

DEFAULT_CONFIG_PATH = os.path.join("config", "config.yaml")

# Optional keys with safe defaults — prevents one-missing-key crashes.
_OPTIONAL_DEFAULTS = {
    "block_size": 1600,
    "min_audio_level": 0.005,
    "max_audio_level": 0.95,
}


class ConfigSection:
    """Attribute-style view over a dict."""

    def __init__(self, data: dict):
        self.__dict__["_data"] = data or {}

    def __getattr__(self, name):
        data = self.__dict__["_data"]
        if name in data:
            value = data[name]
            if isinstance(value, dict):
                return ConfigSection(value)
            return value
        if name in _OPTIONAL_DEFAULTS:
            logger.warning(
                "Config key '%s' missing — using default %r",
                name, _OPTIONAL_DEFAULTS[name])
            return _OPTIONAL_DEFAULTS[name]
        raise AttributeError(
            f"Config has no section/key '{name}'. "
            f"Available: {list(data.keys())}") from None

    def __contains__(self, name):
        return name in self.__dict__["_data"]

    def get(self, name, default=None):
        return self.__dict__["_data"].get(name, default)

    def keys(self):
        return list(self.__dict__["_data"].keys())

    def __repr__(self):
        return f"ConfigSection({self.__dict__['_data']!r})"


class Config(ConfigSection):
    """Backwards-compatible alias: main.py does
    `from agent.config import Config`."""
    pass


def load_config(path: str = DEFAULT_CONFIG_PATH) -> Config:
    """Load YAML config from disk and wrap it in a Config object."""
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Config file not found at '{path}'. "
            f"Run from project root or pass an explicit path.")
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    logger.info("Configuration loaded from %s", path)
    return Config(raw)
