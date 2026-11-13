"""Shared test fixtures.

Modules do `from app.config import settings`, which binds the object at import time.
`get_settings()` is lru_cached, so changing the environment and clearing the cache
creates a NEW Settings object while those modules still reference the old one.

In production the config never changes at runtime, so this is purely a test-harness
concern. `fresh_settings` rebinds the module-level references so tests are
deterministic instead of order-dependent.
"""

from __future__ import annotations

import pytest


def _rebind() -> object:
    import app.autoclaim.policy as policy_mod
    import app.config as config_mod
    import app.http as http_mod

    settings = config_mod.get_settings()
    http_mod.settings = settings
    policy_mod.settings = settings
    return settings


@pytest.fixture
def fresh_settings(monkeypatch):
    """Set env vars, rebuild Settings, and rebind every module-level reference."""

    def _apply(**env: object) -> object:
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        import app.config as config_mod

        config_mod.get_settings.cache_clear()
        return _rebind()

    yield _apply

    import app.config as config_mod

    config_mod.get_settings.cache_clear()
    _rebind()

