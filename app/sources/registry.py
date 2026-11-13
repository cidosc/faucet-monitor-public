"""Source plugin registry.

`enabled_sources` comes from DISCOVERY_ENABLED_SOURCES (default: manual,rss).
Unknown names are reported, not silently ignored.
"""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.logging_conf import get_logger
from app.sources.airdrop_api import AirdropListingSource
from app.sources.airdrops import AirdropFeedSource
from app.sources.base import ProjectCandidate, SourcePlugin
from app.sources.manual import ManualSource
from app.sources.rss import RssSource

log = get_logger(__name__)

# name -> factory(cfg) -> plugin
BUILTIN: dict[str, Any] = {
    "manual": lambda cfg: ManualSource(),
    "rss": lambda cfg: RssSource.from_config(cfg),
    # Structured listings (WordPress custom post type) — the high-signal source.
    "airdrop_api": lambda cfg: AirdropListingSource.from_config(cfg),
    # Editorial feeds are optional and disabled by default in config/sources.yaml.
    "airdrops": lambda cfg: AirdropFeedSource.from_config(cfg),
}


def build_plugins(enabled: list[str] | None = None) -> list[SourcePlugin]:
    enabled = enabled or settings.source_plugin_list
    cfg = ManualSource().load_config()
    plugins: list[SourcePlugin] = []
    for name in enabled:
        factory = BUILTIN.get(name)
        if factory is None:
            log.warning(
                "source_plugin_not_implemented",
                extra={"plugin": name, "available": sorted(BUILTIN)},
            )
            continue
        try:
            plugins.append(factory(cfg))
        except Exception:  # noqa: BLE001
            log.exception("source_plugin_init_failed", extra={"plugin": name})
    return plugins


__all__ = ["BUILTIN", "ProjectCandidate", "SourcePlugin", "build_plugins"]

