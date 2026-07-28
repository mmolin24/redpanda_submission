from __future__ import annotations

import json
import re
from calendar import monthrange
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

SCHEMA_NAMES = (
    "common-defs.schema.json",
    "failure.schema.json",
    "finding.schema.json",
    "ingest-failure.schema.json",
    "monitored-packages.schema.json",
    "release-candidate.schema.json",
    "release-event.schema.json",
)
DEFINITION_ONLY_SCHEMA_NAMES = frozenset({"common-defs.schema.json"})
INSTANCE_SCHEMA_NAMES = tuple(
    name for name in SCHEMA_NAMES if name not in DEFINITION_ONLY_SCHEMA_NAMES
)

JsonObject = dict[str, Any]
_RFC3339_LEAP_SECOND_PATTERN = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])[Tt]"
    r"(?:[01]\d|2[0-3]):[0-5]\d:60"
    r"(?:\.\d+)?(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)$"
)


def read_json(path: Path) -> JsonObject:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def discover_schema_names(root: Path) -> frozenset[str]:
    return frozenset(path.name for path in (root / "schemas").glob("*.schema.json"))


def load_schemas(root: Path) -> dict[str, JsonObject]:
    return {name: read_json(root / "schemas" / name) for name in SCHEMA_NAMES}


def build_registry(
    schemas: dict[str, JsonObject],
) -> Registry:
    resources: list[tuple[str, Resource]] = []
    for name, schema in schemas.items():
        resource = Resource.from_contents(schema)
        resources.extend(
            (
                (schema["$id"], resource),
                (f"file:///schemas/{name}", resource),
            )
        )
    return Registry().with_resources(resources)


def build_format_checker() -> FormatChecker:
    checker = FormatChecker()
    date_time_checker = checker.checkers["date-time"][0]

    @checker.checks("date-time")
    def is_rfc3339_date_time(value: object) -> bool:
        return bool(date_time_checker(value)) or _is_rfc3339_leap_second(value)

    return checker


def _is_rfc3339_leap_second(value: object) -> bool:
    if not isinstance(value, str) or _RFC3339_LEAP_SECOND_PATTERN.fullmatch(value) is None:
        return False
    calendar_check = value[:17] + "59" + value[19:]
    normalized = (
        calendar_check[:-1] + "+00:00" if calendar_check.endswith(("Z", "z")) else calendar_check
    )
    try:
        utc = datetime.fromisoformat(normalized).astimezone(UTC)
    except (OverflowError, ValueError):
        return False
    return (utc.hour, utc.minute, utc.second) == (23, 59, 59) and utc.day == monthrange(
        utc.year, utc.month
    )[1]


def validator_for(
    schema_name: str,
    *,
    schemas: dict[str, JsonObject],
    registry: Registry,
    format_checker: FormatChecker,
) -> Draft202012Validator:
    return Draft202012Validator(
        schemas[schema_name],
        registry=registry,
        format_checker=format_checker,
    )
