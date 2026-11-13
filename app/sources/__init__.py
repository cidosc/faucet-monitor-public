"""Discovery source plugins.

Each plugin proposes candidates only. Trust flags (is_official_source,
automation_allowed) are never set by a plugin.
"""

from app.sources.base import ProjectCandidate, SourcePlugin
from app.sources.registry import BUILTIN, build_plugins

__all__ = ["BUILTIN", "ProjectCandidate", "SourcePlugin", "build_plugins"]

