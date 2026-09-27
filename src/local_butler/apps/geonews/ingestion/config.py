"""Load and validate the primary source configuration."""

from pathlib import Path
from typing import Any

import tomllib


_SOURCE_TYPES = {"rss", "scrape_simple", "scrape_playwright", "telegram"}


def load_sources(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load sources.toml and return a dict keyed by source_id."""
    if path is None:
        path = Path(__file__).parent.parent / "sources.toml"

    with open(path, "rb") as f:
        data = tomllib.load(f)

    sources = data.get("sources", {})
    normalized: dict[str, dict[str, Any]] = {}
    for source_id, config in sources.items():
        config = dict(config)
        config.setdefault("source_id", source_id)
        config.setdefault("enabled", True)
        config.setdefault("priority", 2)
        if config["type"] not in _SOURCE_TYPES:
            raise ValueError(
                f"Source {source_id!r} has unknown type {config['type']!r}"
            )
        normalized[source_id] = config
    return normalized


def enabled_sources(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Return only enabled sources."""
    return {
        k: v for k, v in load_sources(path).items() if v.get("enabled", True)
    }
