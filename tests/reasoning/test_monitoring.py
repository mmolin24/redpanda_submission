from __future__ import annotations

import json
from pathlib import Path

import pytest

from reasoning_worker.monitoring import load_monitored_packages, normalize_package_name


def test_checked_in_config_is_normalized_unique_and_used_for_membership():
    monitored = load_monitored_packages(Path("config/monitored-packages.json"))

    assert monitored.packages == tuple(sorted(set(monitored.packages)))
    assert monitored.contains("Typing_Extensions")
    assert not monitored.contains("not-configured")


def test_package_normalization_matches_pypi_comparison_rules():
    assert normalize_package_name("  Python.Date_utils  ") == "python-date-utils"


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"packages": []}, "1-500"),
        ({"packages": ["requests", "Requests"]}, "unique after normalization"),
        ({"packages": ["requests"], "unexpected": True}, "contain only packages"),
        ({"packages": [42]}, "non-empty string"),
    ],
)
def test_invalid_configs_fail_closed(tmp_path: Path, document, message):
    path = tmp_path / "monitored-packages.json"
    path.write_text(json.dumps(document))

    with pytest.raises(ValueError, match=message):
        load_monitored_packages(path)


def test_missing_config_fails_closed(tmp_path: Path):
    with pytest.raises(ValueError, match="cannot load monitored-package config"):
        load_monitored_packages(tmp_path / "missing-monitored-packages.json")
