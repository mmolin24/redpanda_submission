"""Load and validate the package scope shared with Redpanda Connect."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_PACKAGE_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_MONITORED_PACKAGES = 500


def normalize_package_name(value: str) -> str:
    """Apply the canonical PyPI/PEP 503 comparison form."""
    return re.sub(r"[-_.]+", "-", value.strip()).lower()


@dataclass(frozen=True)
class MonitoredPackages:
    """Hold the normalized package scope shared by ingestion and reasoning."""

    packages: tuple[str, ...]

    def contains(self, package: str) -> bool:
        return normalize_package_name(package) in self.packages


def load_monitored_packages(path: Path) -> MonitoredPackages:
    """Load a fail-closed, normalized monitored-package configuration."""
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load monitored-package config: {path}") from exc
    if not isinstance(document, dict) or set(document) != {"packages"}:
        raise ValueError("monitored-package config must contain only packages")
    raw = document["packages"]
    if not isinstance(raw, list) or not raw or len(raw) > MAX_MONITORED_PACKAGES:
        raise ValueError(f"packages must contain 1-{MAX_MONITORED_PACKAGES} names")
    if not all(isinstance(item, str) and item.strip() for item in raw):
        raise ValueError("every monitored package must be a non-empty string")
    normalized = tuple(normalize_package_name(item) for item in raw)
    if not all(_PACKAGE_NAME.fullmatch(item) for item in normalized):
        raise ValueError("monitored package names must be valid normalized PyPI names")
    if len(set(normalized)) != len(normalized):
        raise ValueError("monitored package names must be unique after normalization")
    return MonitoredPackages(tuple(sorted(normalized)))
