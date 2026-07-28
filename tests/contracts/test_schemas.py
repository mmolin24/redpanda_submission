from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry

from tests.schema_catalog import (
    DEFINITION_ONLY_SCHEMA_NAMES,
    INSTANCE_SCHEMA_NAMES,
    SCHEMA_NAMES,
    build_format_checker,
    build_registry,
    discover_schema_names,
    load_schemas,
    read_json,
    validator_for,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = ROOT / "tests" / "platform" / "fixtures"
POSITIVE_FIXTURES: dict[str, tuple[Path, ...]] = {
    "release-candidate.schema.json": (FIXTURE_ROOT / "release-candidate.valid.json",),
    "release-event.schema.json": (FIXTURE_ROOT / "release-event.valid.json",),
    "finding.schema.json": (FIXTURE_ROOT / "finding.valid.json",),
    "failure.schema.json": (FIXTURE_ROOT / "failure.valid.json",),
    "ingest-failure.schema.json": (FIXTURE_ROOT / "ingest-failure.valid-new.json",),
    "monitored-packages.schema.json": (ROOT / "config" / "monitored-packages.json",),
}
POSITIVE_FIXTURE_CASES = tuple(
    (schema_name, fixture_path)
    for schema_name, fixture_paths in POSITIVE_FIXTURES.items()
    for fixture_path in fixture_paths
)


@pytest.fixture(scope="module")
def schema_catalog() -> dict[str, dict[str, Any]]:
    return load_schemas(ROOT)


@pytest.fixture(scope="module")
def registry(
    schema_catalog: dict[str, dict[str, Any]],
) -> Registry:
    return build_registry(schema_catalog)


@pytest.fixture(scope="module")
def format_checker() -> FormatChecker:
    return build_format_checker()


def _validator(
    schema_name: str,
    schema_catalog: dict[str, dict[str, Any]],
    registry: Registry,
    format_checker: FormatChecker,
) -> Draft202012Validator:
    return validator_for(
        schema_name,
        schemas=schema_catalog,
        registry=registry,
        format_checker=format_checker,
    )


def _assert_rejected_at(
    instance: dict[str, Any],
    *,
    schema_name: str,
    expected_path: tuple[str, ...],
    schema_catalog: dict[str, dict[str, Any]],
    registry: Registry,
    format_checker: FormatChecker,
) -> None:
    errors = list(
        _validator(
            schema_name,
            schema_catalog,
            registry,
            format_checker,
        ).iter_errors(instance)
    )
    assert errors, f"{schema_name} unexpectedly accepted the invalid instance"
    assert any(
        tuple(error.absolute_path)[: len(expected_path)] == expected_path for error in errors
    ), [(tuple(error.absolute_path), error.message) for error in errors]


def test_all_schemas_are_valid_draft_2020_12(
    schema_catalog: dict[str, dict[str, Any]],
) -> None:
    for name, schema in schema_catalog.items():
        assert schema["$schema"] == ("https://json-schema.org/draft/2020-12/schema"), name
        Draft202012Validator.check_schema(schema)


def test_reviewed_schema_catalog_covers_every_checked_in_schema() -> None:
    assert discover_schema_names(ROOT) == frozenset(SCHEMA_NAMES)


def test_every_instance_schema_has_an_authoritative_positive_fixture(
    schema_catalog: dict[str, dict[str, Any]],
) -> None:
    assert set(POSITIVE_FIXTURES) == set(INSTANCE_SCHEMA_NAMES)
    assert set(POSITIVE_FIXTURES).isdisjoint(DEFINITION_ONLY_SCHEMA_NAMES)
    validated_schema_names = {schema_name for schema_name, _fixture_path in POSITIVE_FIXTURE_CASES}
    assert validated_schema_names == set(INSTANCE_SCHEMA_NAMES)

    definition_metadata_keys = {
        "$schema",
        "$id",
        "title",
        "description",
        "$comment",
        "$defs",
    }
    for schema_name in DEFINITION_ONLY_SCHEMA_NAMES:
        schema = schema_catalog[schema_name]
        assert schema.get("$defs")
        assert set(schema).issubset(definition_metadata_keys)


@pytest.mark.parametrize(
    ("format_name", "invalid_value"),
    [
        pytest.param("date-time", "2026-02-30T12:00:00Z", id="date-time"),
        pytest.param("uri", "https://pypi.org/project/pkg with space", id="uri"),
        pytest.param("uuid", "not-a-uuid", id="uuid"),
    ],
)
def test_required_format_checkers_are_active(
    format_checker: FormatChecker,
    format_name: str,
    invalid_value: str,
) -> None:
    assert format_name in format_checker.checkers
    assert not format_checker.conforms(invalid_value, format_name)


@pytest.mark.parametrize(
    "timestamp",
    [
        pytest.param(
            "2026-07-20T08:34:56-04:00",
            id="numeric-offset",
        ),
        pytest.param(
            "2016-12-31T23:59:60Z",
            id="rfc3339-leap-second",
        ),
    ],
)
def test_date_time_checker_preserves_supported_rfc3339_values(
    format_checker: FormatChecker,
    timestamp: str,
) -> None:
    assert format_checker.conforms(timestamp, "date-time")


@pytest.mark.parametrize(
    ("schema_name", "fixture_path"),
    POSITIVE_FIXTURE_CASES,
)
def test_authoritative_positive_fixtures_validate(
    schema_name: str,
    fixture_path: Path,
    schema_catalog: dict[str, dict[str, Any]],
    registry: Registry,
    format_checker: FormatChecker,
) -> None:
    _validator(
        schema_name,
        schema_catalog,
        registry,
        format_checker,
    ).validate(read_json(fixture_path))


@pytest.mark.parametrize(
    ("fixture_name", "expected_path"),
    [
        pytest.param(
            "ingest-failure.invalid-new.json",
            ("payload",),
            id="ingest-failure-evidence",
        ),
    ],
)
def test_authoritative_negative_fixtures_are_rejected(
    fixture_name: str,
    expected_path: tuple[str, ...],
    schema_catalog: dict[str, dict[str, Any]],
    registry: Registry,
    format_checker: FormatChecker,
) -> None:
    schema_name = "ingest-failure.schema.json"
    _assert_rejected_at(
        read_json(FIXTURE_ROOT / fixture_name),
        schema_name=schema_name,
        expected_path=expected_path,
        schema_catalog=schema_catalog,
        registry=registry,
        format_checker=format_checker,
    )


def test_finding_fixture_embeds_the_authoritative_release_event() -> None:
    finding = read_json(FIXTURE_ROOT / "finding.valid.json")
    release_event = read_json(FIXTURE_ROOT / "release-event.valid.json")

    assert finding["source_event"] == release_event


@pytest.mark.parametrize(
    ("schema_name", "fixture_name", "path", "value"),
    [
        pytest.param(
            "release-event.schema.json",
            "release-event.valid.json",
            ("ingested_at",),
            "2026-02-30T12:00:00Z",
            id="release-event-date-time",
        ),
        pytest.param(
            "release-event.schema.json",
            "release-event.valid.json",
            ("release", "url"),
            "https://pypi.org/project/pkg with space",
            id="release-event-uri",
        ),
        pytest.param(
            "failure.schema.json",
            "failure.valid.json",
            ("failure_id",),
            "not-a-uuid",
            id="failure-uuid",
        ),
    ],
)
def test_format_mutations_are_rejected_at_the_expected_field(
    schema_name: str,
    fixture_name: str,
    path: tuple[str, ...],
    value: str,
    schema_catalog: dict[str, dict[str, Any]],
    registry: Registry,
    format_checker: FormatChecker,
) -> None:
    instance = deepcopy(read_json(FIXTURE_ROOT / fixture_name))
    parent = instance
    for segment in path[:-1]:
        child = parent[segment]
        assert isinstance(child, dict)
        parent = child
    parent[path[-1]] = value

    _assert_rejected_at(
        instance,
        schema_name=schema_name,
        expected_path=path,
        schema_catalog=schema_catalog,
        registry=registry,
        format_checker=format_checker,
    )


def test_finding_rejects_an_incomplete_nested_release_event(
    schema_catalog: dict[str, dict[str, Any]],
    registry: Registry,
    format_checker: FormatChecker,
) -> None:
    finding = read_json(FIXTURE_ROOT / "finding.valid.json")
    del finding["source_event"]["release"]

    _assert_rejected_at(
        finding,
        schema_name="finding.schema.json",
        expected_path=("source_event",),
        schema_catalog=schema_catalog,
        registry=registry,
        format_checker=format_checker,
    )


def test_contracts_exclude_known_sensitive_fields(
    schema_catalog: dict[str, dict[str, Any]],
) -> None:
    rendered = json.dumps(schema_catalog).lower()
    assert '"author"' not in rendered
    assert '"description"' not in rendered
    assert "openai_api_key" not in rendered
    assert "authorization" not in rendered
