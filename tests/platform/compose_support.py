from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

_SUBPROCESS_ENVIRONMENT_ALLOWLIST = (
    "LANG",
    "LC_ALL",
    "PATH",
    "TMPDIR",
)


def compose_subprocess_environment(
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return only process settings needed to resolve checked-in Compose."""
    environment = {
        key: os.environ[key] for key in _SUBPROCESS_ENVIRONMENT_ALLOWLIST if key in os.environ
    }
    environment.update(overrides or {})
    return environment


def decode_json_object(payload: str, *, source: str) -> dict[str, Any]:
    """Decode a configuration subprocess result and enforce an object root."""
    return _require_object(json.loads(payload), source=source)


def _require_object(value: object, *, source: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{source} must produce a JSON object")
    return value


def load_compose_model(
    root: Path,
    *,
    overrides: dict[str, str] | None = None,
    include_all_profiles: bool = False,
    additional_files: tuple[Path, ...] = (),
) -> dict[str, Any]:
    """Resolve Compose without loading the developer's .env or credentials."""
    with tempfile.NamedTemporaryFile() as empty_environment:
        command = [
            "docker",
            "compose",
            "--env-file",
            empty_environment.name,
            "-f",
            str(root / "docker-compose.yml"),
        ]
        for additional_file in additional_files:
            command.extend(("-f", str(additional_file)))
        if include_all_profiles:
            command.extend(("--profile", "*"))
        command.extend(("config", "--format", "json"))
        result = subprocess.run(
            command,
            cwd=root,
            env=compose_subprocess_environment(overrides),
            check=True,
            capture_output=True,
            text=True,
        )
    return decode_json_object(result.stdout, source="docker compose config")


def load_yaml_document(path: Path) -> dict[str, Any]:
    """Safely parse checked-in YAML from the locked platform-test environment."""
    source = f"YAML document {path}"
    try:
        value: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ValueError(f"{source} could not be parsed as safe YAML") from None
    return _require_object(value, source=source)
