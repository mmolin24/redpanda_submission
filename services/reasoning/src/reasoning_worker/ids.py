"""Generate deterministic identifiers for releases, analyses, and attempts."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


def canonical_json(value: Any) -> str:
    """Serialize a JSON-compatible value deterministically."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(value: Any) -> str:
    """Hash a JSON-compatible value using its canonical representation."""
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


def deterministic_id(*parts: str) -> str:
    """Hash ordered identity parts into a stable opaque identifier."""
    return "sha256:" + hashlib.sha256(":".join(parts).encode()).hexdigest()


def opaque_id() -> str:
    """Create an occurrence identifier that does not reveal domain data."""
    # The standard library gains uuid7 after the worker's minimum runtime; UUID4
    # remains opaque and collision-safe without embedding package/user data.
    return str(uuid.uuid4())
