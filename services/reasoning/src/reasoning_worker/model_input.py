"""Compile bounded evidence views for model-assisted reasoning stages."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .ids import canonical_json, sha256_json
from .sanitization import sanitize

REASONING_MODEL_INPUT_MAX_BYTES = 48 * 1024
EVIDENCE_COLLECTION_MAX_BYTES = 32 * 1024
EVIDENCE_COLLECTION_MAX_ITEMS = 500
EVIDENCE_FACT_VALUE_MAX_BYTES = 8 * 1024
PROVIDER_USER_JSON_MAX_BYTES = 64 * 1024


class ModelInputTooLarge(RuntimeError):
    """A complete structured request exceeds the hard provider-side budget."""


@dataclass(frozen=True)
class CompiledModelInput:
    """Hold one sanitized request and its canonical byte accounting."""

    value: dict[str, Any]
    canonical_json: str
    encoded_bytes: int


@dataclass(frozen=True)
class BoundedCollection:
    """Return selected whole values and a manifest of omitted content."""

    items: tuple[Any, ...]
    summary: dict[str, Any]


def encoded_json_bytes(value: Any) -> int:
    """Measure the canonical UTF-8 JSON representation of a value."""
    return len(canonical_json(value).encode("utf-8"))


def compile_model_input(
    value: dict[str, Any],
    *,
    max_bytes: int = PROVIDER_USER_JSON_MAX_BYTES,
) -> CompiledModelInput:
    """Sanitize structured values, then serialize once without slicing JSON."""
    safe_value = sanitize(value)
    if not isinstance(safe_value, dict):
        raise TypeError("model input must be a JSON object")
    rendered = canonical_json(safe_value)
    encoded_bytes = len(rendered.encode("utf-8"))
    if encoded_bytes > max_bytes:
        raise ModelInputTooLarge(
            f"model input user JSON exceeds the {max_bytes}-byte provider budget"
        )
    return CompiledModelInput(safe_value, rendered, encoded_bytes)


def bound_collection(
    values: Iterable[Any],
    *,
    max_bytes: int = EVIDENCE_COLLECTION_MAX_BYTES,
    max_items: int = EVIDENCE_COLLECTION_MAX_ITEMS,
    selection_key: Callable[[Any], Any] | None = None,
    manifest_values: Iterable[Any] | None = None,
) -> BoundedCollection:
    """Select whole values deterministically and describe the complete set."""
    original = list(values)
    manifest = list(manifest_values) if manifest_values is not None else original
    if len(manifest) != len(original):
        raise ValueError("manifest_values must have the same item count as values")
    candidates = sorted(original, key=selection_key) if selection_key else original
    included: list[Any] = []
    included_bytes = 2  # JSON array delimiters.
    for item in candidates:
        item_bytes = encoded_json_bytes(item)
        candidate_bytes = included_bytes + item_bytes + (1 if included else 0)
        if len(included) < max_items and candidate_bytes <= max_bytes:
            included.append(item)
            included_bytes = candidate_bytes

    full_bytes = encoded_json_bytes(manifest)
    transformed_count = sum(
        canonical_json(item) != canonical_json(source)
        for item, source in zip(original, manifest, strict=True)
    )
    summary = {
        "omitted": len(included) != len(original) or transformed_count > 0,
        "total_count": len(manifest),
        "included_count": len(included),
        "omitted_count": len(manifest) - len(included),
        "full_content_bytes": full_bytes,
        "full_content_sha256": sha256_json(manifest),
    }
    if transformed_count:
        summary["summarized_count"] = transformed_count
    return BoundedCollection(tuple(included), summary)


def summarize_oversized_value(
    value: Any,
    *,
    max_bytes: int = EVIDENCE_FACT_VALUE_MAX_BYTES,
) -> Any:
    """Replace an oversized fact value with a complete, deterministic manifest."""
    content_bytes = encoded_json_bytes(value)
    if content_bytes <= max_bytes:
        return value
    return {
        "omitted": True,
        "reason": "value_exceeds_model_budget",
        "full_content_bytes": content_bytes,
        "full_content_sha256": sha256_json(value),
    }
