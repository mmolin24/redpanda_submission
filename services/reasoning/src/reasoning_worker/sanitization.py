"""Redact secrets and bound untrusted values before persistence or logging."""

from __future__ import annotations

import base64
import binascii
import json
import re
from bisect import insort
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from .models import Json

SanitizationCategory = Literal[
    "basic_auth",
    "bearer_token",
    "github_token",
    "openai_token",
    "private_key",
    "secret_field",
    "url_credential",
]

_POLICY_VERSION = "evidence-sanitization.v1"
_MAX_REPORTED_LOCATIONS = 20
_MAX_REPORTED_PATH_DEPTH = 12
_MAX_REPORTED_PATH_LENGTH = 160
_TRUNCATED_PATH = "$.[truncated]"
_CATEGORIES: tuple[SanitizationCategory, ...] = (
    "basic_auth",
    "bearer_token",
    "github_token",
    "openai_token",
    "private_key",
    "secret_field",
    "url_credential",
)
_SECRET_KEY = re.compile(
    r"(?:^|_)(?:api_key|private_key|secret_key|authorization|cookie|"
    r"credentials?|passwd|password|secret|token)$"
)
_CAMEL_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_WORD_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_FIELD_DELIMITERS = re.compile(r"[^A-Za-z0-9]+")
_NON_SECRET_FIELDS = frozenset(
    {
        "has_secret",
        "has_token",
        "is_secret",
        "not_secret",
        "notsecret",
        "secret_count",
        "token_count",
    }
)
_REPORT_PATH_SEGMENTS = frozenset(
    {
        "added",
        "after",
        "archive_sha256",
        "artifact_manifest_diff",
        "baseline",
        "before",
        "candidate",
        "changed",
        "classifiers",
        "code_hunks",
        "computed",
        "content_sha256",
        "context",
        "diff",
        "documents",
        "filename",
        "files",
        "files_diff",
        "license_expression",
        "metadata",
        "missing",
        "name",
        "packagetype",
        "path",
        "provenance",
        "python_version",
        "removed",
        "requires_dist",
        "requires_dist_diff",
        "requires_python",
        "requires_python_diff",
        "retrieved_at",
        "sha256",
        "size",
        "source_url",
        "upload_time_iso_8601",
        "version",
        "vulnerabilities",
        "vulnerability_diff",
        "yank_diff",
        "yanked",
        "yanked_reason",
    }
)
_PEM_BOUNDARY = re.compile(
    r"-----(BEGIN|END) "
    r"(ENCRYPTED PRIVATE KEY|PRIVATE KEY|RSA PRIVATE KEY|EC PRIVATE KEY|"
    r"DSA PRIVATE KEY|OPENSSH PRIVATE KEY)-----"
)
# These strings are disclosure markers, not credentials.
_REDACTED_SECRET_FIELD = "[REDACTED:secret-field]"  # noqa: S105
_REDACTED_PRIVATE_KEY = "[REDACTED:private-key]"


@dataclass(frozen=True)
class _EmbeddedSecretPattern:
    category: SanitizationCategory
    pattern: re.Pattern[str]
    replacement: str
    validator: Callable[[re.Match[str]], bool] | None = None


def _character_class_count(value: str) -> int:
    return sum(
        (
            any(character.islower() for character in value),
            any(character.isupper() for character in value),
            any(character.isdigit() for character in value),
        )
    )


def _looks_like_openai_token(match: re.Match[str]) -> bool:
    prefix = match.group("prefix")
    payload = match.group("payload")
    if prefix == "sk-proj-":
        return len(payload) >= 80 and _character_class_count(payload) == 3
    return len(payload) == 48 and payload.isalnum() and _character_class_count(payload) >= 2


def _looks_like_github_token(match: re.Match[str]) -> bool:
    return _character_class_count(match.group("credential")) >= 2


def _looks_like_bearer_credential(match: re.Match[str]) -> bool:
    segments = match.group("credential").split(".")
    if len(segments) != 3 or len(segments[2]) < 8:
        return False
    decoded: list[object] = []
    try:
        for segment in segments[:2]:
            padding = "=" * (-len(segment) % 4)
            raw = base64.urlsafe_b64decode(f"{segment}{padding}")
            decoded.append(json.loads(raw.decode("utf-8")))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    return all(isinstance(value, dict) for value in decoded)


def _looks_like_basic_credential(match: re.Match[str]) -> bool:
    encoded = match.group("credential")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return False
    username, separator, password = decoded.partition(b":")
    return bool(username and separator and password)


_EMBEDDED_SECRET_PATTERNS: tuple[_EmbeddedSecretPattern, ...] = (
    _EmbeddedSecretPattern(
        "openai_token",
        re.compile(
            r"(?<![A-Za-z0-9_-])(?P<prefix>sk-(?:proj-)?)"
            r"(?P<payload>[A-Za-z0-9_-]{20,})(?![A-Za-z0-9_-])"
        ),
        "[REDACTED:openai-token]",
        _looks_like_openai_token,
    ),
    _EmbeddedSecretPattern(
        "github_token",
        re.compile(
            r"(?<![A-Za-z0-9_])"
            r"(?P<credential>"
            r"(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_11[A-Za-z0-9_]{60,}))"
            r"(?![A-Za-z0-9_])"
        ),
        "[REDACTED:github-token]",
        _looks_like_github_token,
    ),
    _EmbeddedSecretPattern(
        "bearer_token",
        re.compile(
            r"(?i)\b(?P<prefix>Bearer[ \t]+)"
            r"(?P<credential>[A-Za-z0-9_-]{8,}\."
            r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
            r"(?=$|[\s,;)\]}])"
        ),
        r"\g<prefix>[REDACTED:bearer-token]",
        _looks_like_bearer_credential,
    ),
    _EmbeddedSecretPattern(
        "basic_auth",
        re.compile(
            r"(?i)\b(?P<prefix>Authorization[ \t]*:[ \t]*Basic[ \t]+)"
            r"(?P<credential>[A-Za-z0-9+/]{12,}={0,2})"
            r"(?=$|[\s,;)\]}])"
        ),
        r"\g<prefix>[REDACTED:basic-auth]",
        _looks_like_basic_credential,
    ),
    _EmbeddedSecretPattern(
        "url_credential",
        re.compile(r"(://)[^/\s@]+@"),
        r"\1[REDACTED:url-credential]@",
    ),
)


@dataclass(frozen=True, order=True)
class SanitizationLocation:
    """Identify where one category of sensitive content was redacted."""

    category: SanitizationCategory
    path: str

    def to_dict(self) -> Json:
        return {"category": self.category, "path": self.path}


@dataclass(frozen=True)
class SanitizationReport:
    """Summarize redactions without retaining the sensitive values."""

    total_redactions: int
    category_counts: dict[SanitizationCategory, int]
    locations: tuple[SanitizationLocation, ...]
    omitted_location_count: int
    policy_version: str = _POLICY_VERSION

    @property
    def locations_truncated(self) -> bool:
        return self.omitted_location_count > 0

    def to_dict(self) -> Json:
        return {
            "policy_version": self.policy_version,
            "total_redactions": self.total_redactions,
            "category_counts": {
                category: self.category_counts[category] for category in _CATEGORIES
            },
            "locations": [location.to_dict() for location in self.locations],
            "reported_location_count": len(self.locations),
            "omitted_location_count": self.omitted_location_count,
            "locations_truncated": self.locations_truncated,
        }


@dataclass(frozen=True)
class SanitizationResult:
    """Return a sanitized value together with its bounded audit report."""

    value: Any
    report: SanitizationReport


@dataclass
class _ReportAccumulator:
    total_redactions: int = 0
    category_counts: dict[SanitizationCategory, int] = field(
        default_factory=lambda: dict.fromkeys(_CATEGORIES, 0)
    )
    locations: list[SanitizationLocation] = field(default_factory=list)

    def record(
        self,
        category: SanitizationCategory,
        path: str,
        count: int = 1,
    ) -> None:
        if count <= 0:
            return
        self.total_redactions += count
        self.category_counts[category] += count
        location = SanitizationLocation(category, path)
        for _ in range(min(count, _MAX_REPORTED_LOCATIONS)):
            insort(self.locations, location)
            if len(self.locations) > _MAX_REPORTED_LOCATIONS:
                self.locations.pop()

    def report(self) -> SanitizationReport:
        return SanitizationReport(
            total_redactions=self.total_redactions,
            category_counts=dict(self.category_counts),
            locations=tuple(self.locations),
            omitted_location_count=self.total_redactions - len(self.locations),
        )


def _bounded_path(candidate: str) -> str:
    depth = candidate.count(".") + candidate.count("[")
    if depth > _MAX_REPORTED_PATH_DEPTH or len(candidate) > _MAX_REPORTED_PATH_LENGTH:
        return _TRUNCATED_PATH
    return candidate


def _dynamic_path(parent: str) -> str:
    if parent == _TRUNCATED_PATH or parent.endswith(".*"):
        return parent
    return _bounded_path(f"{parent}.*")


def _path_for_key(parent: str, key: object) -> str:
    segment = str(key)
    if segment not in _REPORT_PATH_SEGMENTS:
        return _dynamic_path(parent)
    return _bounded_path(f"{parent}.{segment}")


def _path_for_index(parent: str, index: int) -> str:
    if parent == _TRUNCATED_PATH:
        return parent
    return _bounded_path(f"{parent}[{index}]")


def _is_secret_field(key: str) -> bool:
    separated = _CAMEL_ACRONYM_BOUNDARY.sub(r"\1_\2", key)
    separated = _CAMEL_WORD_BOUNDARY.sub(r"\1_\2", separated)
    normalized = _FIELD_DELIMITERS.sub("_", separated).strip("_").lower()
    return normalized not in _NON_SECRET_FIELDS and _SECRET_KEY.search(normalized) is not None


def _is_sanitization_category_count_map(value: dict[object, object]) -> bool:
    return set(value) == set(_CATEGORIES) and all(
        type(count) is int and count >= 0 for count in value.values()
    )


def _sanitized_entry_sort_key(entry: tuple[str, Any]) -> tuple[str, str]:
    key, value = entry
    encoded_value = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return key, encoded_value


def _redact_private_keys(
    value: str,
    *,
    path: str,
    report: _ReportAccumulator,
) -> str:
    ranges: list[tuple[int, int]] = []
    active_start: int | None = None
    active_depth = 0
    for match in _PEM_BOUNDARY.finditer(value):
        boundary_kind = match.group(1)
        if boundary_kind == "BEGIN":
            if active_start is None:
                active_start = match.start()
            active_depth += 1
        elif active_start is not None:
            active_depth -= 1
            if active_depth == 0:
                ranges.append((active_start, match.end()))
                active_start = None
    if active_start is not None:
        ranges.append((active_start, len(value)))
    if not ranges:
        return value

    parts: list[str] = []
    cursor = 0
    for start, end in ranges:
        parts.extend((value[cursor:start], _REDACTED_PRIVATE_KEY))
        cursor = end
    parts.append(value[cursor:])
    report.record("private_key", path, len(ranges))
    return "".join(parts)


def _redact_text(
    value: str,
    *,
    path: str,
    report: _ReportAccumulator,
) -> str:
    redacted = _redact_private_keys(value, path=path, report=report)
    for secret_pattern in _EMBEDDED_SECRET_PATTERNS:
        parts: list[str] = []
        cursor = 0
        count = 0
        for match in secret_pattern.pattern.finditer(redacted):
            if secret_pattern.validator is not None and not secret_pattern.validator(match):
                continue
            parts.extend(
                (
                    redacted[cursor : match.start()],
                    match.expand(secret_pattern.replacement),
                )
            )
            cursor = match.end()
            count += 1
        if count:
            parts.append(redacted[cursor:])
            redacted = "".join(parts)
            report.record(secret_pattern.category, path, count)
    return redacted


def _sanitize(value: Any, *, path: str, report: _ReportAccumulator) -> Any:
    if isinstance(value, dict):
        trusted_category_counts = _is_sanitization_category_count_map(value)
        entries: list[tuple[str, Any]] = []
        for key, item in value.items():
            string_key = str(key)
            sanitized_key = _redact_text(
                string_key,
                path=_dynamic_path(path),
                report=report,
            )
            child_path = _path_for_key(path, sanitized_key)
            if not trusted_category_counts and _is_secret_field(string_key):
                sanitized_item = _REDACTED_SECRET_FIELD
                report.record("secret_field", child_path)
            else:
                sanitized_item = _sanitize(item, path=child_path, report=report)
            entries.append((sanitized_key, sanitized_item))

        result: Json = {}
        for sanitized_key, sanitized_item in sorted(
            entries,
            key=_sanitized_entry_sort_key,
        ):
            unique_key = sanitized_key
            duplicate_number = 2
            while unique_key in result:
                unique_key = f"{sanitized_key}#duplicate-{duplicate_number}"
                duplicate_number += 1
            result[unique_key] = sanitized_item
        return result
    if isinstance(value, (list, tuple)):
        return [
            _sanitize(item, path=_path_for_index(path, index), report=report)
            for index, item in enumerate(value)
        ]
    if isinstance(value, str):
        return _redact_text(value, path=path, report=report)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value), path=path, report=report)


def sanitize_with_report(value: Any) -> SanitizationResult:
    """Redact high-confidence credentials and return bounded non-secret metadata."""
    accumulator = _ReportAccumulator()
    sanitized = _sanitize(value, path="$", report=accumulator)
    return SanitizationResult(value=sanitized, report=accumulator.report())


def sanitize(value: Any) -> Any:
    """Redact secret-bearing values without omitting or truncating content."""
    return sanitize_with_report(value).value


def bounded_text(value: str, *, max_characters: int) -> str:
    """Apply an explicit character limit for a size-constrained text contract."""
    if max_characters <= 0:
        raise ValueError("max_characters must be positive")
    return value[:max_characters]
