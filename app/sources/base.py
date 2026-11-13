"""Source plugin protocol.

A source plugin's ONLY job is to propose candidate projects/faucets. It never decides
whether automation is allowed, never asserts a faucet is official, and never touches
the chain. Those judgements belong to a human (see app/autoclaim/policy.py).

Adding a new data source = drop a module in this package and register it in
`app/sources/registry.py`. No core code changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ProjectCandidate:
    """A proposed project, straight from a source. All trust flags default to false."""

    slug: str
    name: str
    source_plugin: str
    source_url: str
    chain_key: str | None = None
    network: str | None = None
    project_url: str | None = None
    docs_url: str | None = None
    github_url: str | None = None
    faucet_urls: list[str] = field(default_factory=list)
    notes: str | None = None
    # If set, the discover job files a MANUAL_REQUIRED task so the lead reaches a human
    # instead of sitting silently in the database.
    task_title: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SourcePlugin(Protocol):
    name: str

    async def discover(self) -> list[ProjectCandidate]:
        """Return candidates. Must be idempotent and must not raise on partial failure."""

