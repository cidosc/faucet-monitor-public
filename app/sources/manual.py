"""Manual source: a human-curated YAML file.

Highest trust level. Automated plugins add candidates on top of this; they never
override an entry that appears here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.config import settings
from app.logging_conf import get_logger
from app.sources.base import ProjectCandidate

log = get_logger(__name__)


class ManualSource:
    name = "manual"

    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path or settings.sources_config_path)

    def load_config(self) -> dict[str, Any]:
        if not self.path.exists():
            log.warning("sources_config_missing", extra={"path": str(self.path)})
            return {}
        try:
            return yaml.safe_load(self.path.read_text()) or {}
        except Exception as exc:  # noqa: BLE001
            log.error("sources_config_invalid", extra={"path": str(self.path), "error": str(exc)})
            return {}

    async def discover(self) -> list[ProjectCandidate]:
        cfg = self.load_config()
        out: list[ProjectCandidate] = []
        for entry in cfg.get("projects") or []:
            slug = entry.get("slug")
            if not slug:
                continue
            faucets = entry.get("faucets") or []
            urls: list[str] = []
            for f in faucets:
                if isinstance(f, str):
                    urls.append(f)
                elif isinstance(f, dict) and f.get("url"):
                    urls.append(f["url"])
            out.append(
                ProjectCandidate(
                    slug=slug,
                    name=entry.get("name", slug),
                    source_plugin=self.name,
                    source_url=entry.get("source_url") or str(self.path),
                    chain_key=entry.get("chain"),
                    network=entry.get("network"),
                    project_url=entry.get("project_url"),
                    docs_url=entry.get("docs_url"),
                    github_url=entry.get("github_url"),
                    faucet_urls=urls,
                    notes=entry.get("notes"),
                    raw={
                        "automation_allowed": entry.get("automation_allowed", "UNKNOWN"),
                        "automation_evidence": entry.get("automation_evidence"),
                    },
                )
            )
        log.info("manual_source_loaded", extra={"candidates": len(out)})
        return out

    # automation flags from this file are applied by the discover job (human-authored,
    # so they are trusted — unlike anything an automated plugin produces).

