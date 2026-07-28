"""Load and validate API runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum


class SourceMode(StrEnum):
    """Identify how release records enter the running stack."""

    FIXTURE = "fixture"
    HISTORY = "history"
    LIVE = "live"


_SOURCE_MODES = {
    "source-fixture.yaml": SourceMode.FIXTURE,
    "source-history.yaml": SourceMode.HISTORY,
    "source-live.yaml": SourceMode.LIVE,
}


@dataclass(frozen=True, slots=True)
class Settings:
    """Hold validated configuration for the read-only API process."""

    database_url: str
    grafana_base_url: str
    source_config: str = "source-fixture.yaml"
    api_title: str = "PyPI Change Intelligence API"

    def __post_init__(self) -> None:
        if self.source_config not in _SOURCE_MODES:
            allowed = ", ".join(sorted(_SOURCE_MODES))
            raise ValueError(f"SOURCE_CONFIG must be one of: {allowed}")

    @property
    def source_mode(self) -> SourceMode:
        return _SOURCE_MODES[self.source_config]

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.getenv(
                "DATABASE_URL",
                "postgresql://pypi:pypi@postgres:5432/pypi_intelligence",
            ),
            grafana_base_url=os.getenv("GRAFANA_BASE_URL", "http://localhost:3001"),
            source_config=os.getenv("SOURCE_CONFIG", "source-fixture.yaml"),
        )
