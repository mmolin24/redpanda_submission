"""Run the complete fixture stack in one disposable, project-scoped sandbox."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .docker_project import DockerProject, DockerProjectError
from .process import (
    ProcessFailure,
    ProcessInterrupted,
    run_capture,
    termination_signal_scope,
)

_DOCKER_ENDPOINT = "unix:///var/run/docker.sock"
_PORT_ENVIRONMENT_NAMES = (
    "REDPANDA_ADMIN_PORT",
    "REDPANDA_CONSOLE_PORT",
    "CONNECT_SOURCE_PORT",
    "REASONING_HEALTH_PORT",
    "REASONING_METRICS_PORT",
    "CONNECT_SINK_PORT",
    "CONNECT_TRACE_BRIDGE_PORT",
    "API_PORT",
    "WEB_PORT",
    "TEMPO_PORT",
    "LOKI_PORT",
    "PROMETHEUS_PORT",
    "GRAFANA_PORT",
)
_IMAGE_ENVIRONMENT_NAMES = {
    "REASONING_IMAGE": "reasoning",
    "API_IMAGE": "api",
    "WEB_IMAGE": "web",
    "ALLOY_IMAGE": "alloy",
    "TEMPO_IMAGE": "tempo",
    "LOKI_IMAGE": "loki",
}
_ENVIRONMENT_ALLOWLIST = ("LANG", "LC_ALL", "PATH")
_SAFE_ENVIRONMENT = {
    "EVIDENCE_MODE": "fixture",
    "MODEL_MODE": "fake",
    "FAKE_MODEL_DELAY_SECONDS": "0",
    "ANALYSIS_POLICY_REVISION": "analysis-policy-v1",
    "SOURCE_CONFIG": "source-fixture.yaml",
    "SOURCE_FIXTURE_SCENARIO": "ingestion-relevance",
    "SMOKE_SCENARIO": "ingestion-relevance",
    "OPENAI_API_KEY": "",
    "OPENAI_PROMPT_CACHE_ENABLED": "false",
    "OBS_CAPTURE_MODEL_PAYLOADS": "false",
    "OPENAI_TIMEOUT_SECONDS": "60",
    "PROCESSING_RETRY_BACKOFF_SECONDS": "5",
    "PROCESSING_RETRY_MAX_ELAPSED_SECONDS": "300",
    "CONSUMER_MAX_POLL_INTERVAL_MS": "900000",
}
_PROJECT_PATTERN = re.compile(r"pypi-fixture-smoke-[0-9a-f]{32}")
_IMAGE_PATTERN = re.compile(r"pypi-fixture-smoke-[0-9a-f]{32}/[a-z-]+:local")
_CONTAINER_ID_PATTERN = re.compile(r"[0-9a-f]{64}")
_CANARY_IMAGE = (
    "busybox:1.37.0-uclibc@sha256:39e0df8c4d65953b55c344f017e1ff2e0031a7454b3c24e6b76d402f207e315a"
)
_OPERATION_TIMEOUT_SECONDS = 2_700
_DIAGNOSTIC_TIMEOUT_SECONDS = 60
_PROJECT_CLEANUP_TIMEOUT_SECONDS = 300
_CANARY_CLEANUP_TIMEOUT_SECONDS = 120
_IMAGE_CLEANUP_TIMEOUT_SECONDS = 150
_MOVEMENT_STAGE_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_COMPOSE_EXECUTABLE_CANDIDATES = (
    Path("/usr/libexec/docker/cli-plugins/docker-compose"),
    Path("/usr/lib/docker/cli-plugins/docker-compose"),
    Path("/usr/local/lib/docker/cli-plugins/docker-compose"),
    Path("/usr/local/libexec/docker/cli-plugins/docker-compose"),
    Path("/opt/homebrew/lib/docker/cli-plugins/docker-compose"),
    Path("/Applications/Docker.app/Contents/Resources/cli-plugins/docker-compose"),
)


class IsolatedSmokeError(RuntimeError):
    """A stable, non-secret isolated-smoke failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _Deadline:
    """Cap every operation in one phase to a shared monotonic deadline."""

    expires_at: float
    failure_code: str
    monotonic: Callable[[], float]

    @classmethod
    def after(
        cls,
        seconds: float,
        failure_code: str,
        monotonic: Callable[[], float],
    ) -> _Deadline:
        return cls(monotonic() + seconds, failure_code, monotonic)

    def cap(self, requested_seconds: float) -> float:
        remaining = self.expires_at - self.monotonic()
        if remaining <= 0:
            raise IsolatedSmokeError(self.failure_code)
        return min(requested_seconds, remaining)


@dataclass(frozen=True)
class SmokeSettings:
    """One complete immutable scope for a disposable smoke run."""

    root: Path
    project: str
    owner: str
    compose_executable: Path
    environment: dict[str, str]
    image_tags: tuple[str, ...]
    canary_project: str
    canary_owner: str
    canary_marker: str

    @property
    def compose(self) -> tuple[str, ...]:
        return (
            str(self.compose_executable),
            "--project-name",
            self.project,
            "--env-file",
            "/dev/null",
            "--file",
            str(self.root / "docker-compose.yml"),
            "--file",
            str(self.root / "infra" / "smoke.compose.yml"),
            "--project-directory",
            str(self.root),
        )


def build_settings(
    root: Path,
    temporary: Path,
    *,
    scenario: str = "ingestion-relevance",
    token_hex: Callable[[int], str] = secrets.token_hex,
    parent_environment: Mapping[str, str] | None = None,
) -> SmokeSettings:
    """Create an unguessable project with a credential-free child environment."""
    validated_root = _validated_root(root)
    project_token = token_hex(16)
    owner = token_hex(16)
    if (
        re.fullmatch(r"[0-9a-f]{32}", project_token) is None
        or re.fullmatch(r"[0-9a-f]{32}", owner) is None
    ):
        raise IsolatedSmokeError("smoke_random_identity_invalid")
    project = f"pypi-fixture-smoke-{project_token}"
    if _PROJECT_PATTERN.fullmatch(project) is None:
        raise IsolatedSmokeError("smoke_random_identity_invalid")
    canary_seed = hashlib.sha256(f"{project}\0{owner}\0canary".encode()).hexdigest()
    canary_project = f"pypi-foreign-canary-{canary_seed[:32]}"
    canary_owner = hashlib.sha256(f"{canary_seed}\0owner".encode()).hexdigest()[:32]
    canary_marker = f"foreign-marker-{canary_seed[32:64]}"

    source_environment = parent_environment if parent_environment is not None else os.environ
    environment = {
        key: source_environment[key] for key in _ENVIRONMENT_ALLOWLIST if key in source_environment
    }
    docker_config = temporary / "docker-config"
    isolated_home = temporary / "home"
    isolated_tmp = temporary / "tmp"
    for directory in (docker_config, isolated_home, isolated_tmp):
        directory.mkdir(mode=0o700)

    image_environment = {
        variable: f"{project}/{logical_name}:local"
        for variable, logical_name in _IMAGE_ENVIRONMENT_NAMES.items()
    }
    image_tags = tuple(sorted(image_environment.values()))
    compose_executable, plugin_directory = _resolve_compose_toolchain()
    docker_cli_configuration = docker_config / "config.json"
    docker_cli_configuration.write_text(
        json.dumps(
            {"cliPluginsExtraDirs": [str(plugin_directory)]},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    docker_cli_configuration.chmod(0o600)
    environment.update(
        {
            **_SAFE_ENVIRONMENT,
            # An empty host port asks Compose/Docker to allocate an ephemeral
            # port while the explicit 127.0.0.1 binding remains in force.
            **dict.fromkeys(_PORT_ENVIRONMENT_NAMES, ""),
            **image_environment,
            "CI": "true",
            "COMPOSE_PROJECT_NAME": project,
            "DOCKER_CONFIG": str(docker_config),
            "DOCKER_HOST": _DOCKER_ENDPOINT,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(isolated_home),
            "NO_COLOR": "1",
            "NPM_CONFIG_USERCONFIG": "/dev/null",
            "PYPI_SMOKE_OWNER": owner,
            "SMOKE_COMPOSE_FILE": str(validated_root / "docker-compose.yml"),
            "SMOKE_COMPOSE_OVERRIDE_FILE": str(validated_root / "infra" / "smoke.compose.yml"),
            "SMOKE_COMPOSE_EXECUTABLE": str(compose_executable),
            "SMOKE_FOREIGN_MARKER": canary_marker,
            "SMOKE_PROGRESS_FILE": str(temporary / "movement-stage"),
            "SMOKE_PROJECT_DIRECTORY": str(validated_root),
            "SMOKE_PROJECT_NAME": project,
            "TMPDIR": str(isolated_tmp),
            "UV_NO_CONFIG": "1",
        }
    )
    if scenario == "exact-release-missing":
        environment.update(
            {
                "EVIDENCE_MODE": "pypi",
                "SOURCE_FIXTURE_SCENARIO": scenario,
                "SMOKE_SCENARIO": scenario,
            }
        )
    elif scenario not in {
        "ingestion-relevance",
        "duplicate-terminal-replay",
        "analysis-version-replay",
        "deterministic-finding-ui",
        "end-to-end-trace",
        "runtime-signals",
        "full-stack-drain",
        "worker-shutdown",
    }:
        raise IsolatedSmokeError("smoke_scenario_invalid")
    else:
        environment["SMOKE_SCENARIO"] = scenario
    return SmokeSettings(
        validated_root,
        project,
        owner,
        compose_executable,
        environment,
        image_tags,
        canary_project,
        canary_owner,
        canary_marker,
    )


def run(
    settings: SmokeSettings,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Execute and clean one isolated complete-stack smoke run."""
    deadline = _Deadline.after(
        _OPERATION_TIMEOUT_SECONDS,
        "smoke_operation_deadline_exceeded",
        monotonic,
    )

    def command_runner(
        command: tuple[str, ...],
        timeout_seconds: float,
        failure_code: str,
    ) -> str:
        try:
            return run_capture(
                command,
                settings.root,
                settings.environment,
                timeout_seconds=deadline.cap(timeout_seconds),
                failure_code=failure_code,
            )
        except IsolatedSmokeError as error:
            raise DockerProjectError(error.code) from None
        except ProcessFailure as error:
            raise DockerProjectError(error.code) from None

    def smoke_command_runner(
        command: tuple[str, ...],
        timeout_seconds: float,
        failure_code: str,
    ) -> str:
        return _run_command(
            settings,
            command,
            timeout_seconds=deadline.cap(timeout_seconds),
            failure_code=failure_code,
        )

    project = DockerProject(
        endpoint=_DOCKER_ENDPOINT,
        project=settings.project,
        owner=settings.owner,
        run=command_runner,
    )
    canary = DockerProject(
        endpoint=_DOCKER_ENDPOINT,
        project=settings.canary_project,
        owner=settings.canary_owner,
        run=command_runner,
    )
    primary_error: BaseException | None = None
    diagnostics: tuple[dict[str, str], ...] = ()
    try:
        project.assert_absent()
        canary.assert_absent()
        _assert_images_absent(settings, smoke_command_runner)
        model = smoke_command_runner(
            (*settings.compose, "config", "--format", "json"),
            120,
            "smoke_compose_invalid",
        )
        _validate_model(settings, model)
        smoke_command_runner(
            (*settings.compose, "build"),
            1_800,
            "smoke_build_failed",
        )
        _start_canary(settings, canary, smoke_command_runner)
        project.reserve_networks()
        smoke_command_runner(
            (*settings.compose, "create"),
            300,
            "smoke_create_failed",
        )
        smoke_command_runner(
            (*settings.compose, "start", "--wait", "--wait-timeout", "900"),
            1_800,
            "smoke_start_failed",
        )
        _configure_grafana_public_url(settings, smoke_command_runner)
        smoke_command_runner(
            ("sh", str(settings.root / "infra" / "smoke.sh")),
            600,
            "smoke_movement_failed",
        )
        _verify_foreign_marker_absent(settings, smoke_command_runner, deadline)
    except BaseException as error:
        primary_error = error
        deadline = _Deadline.after(
            _DIAGNOSTIC_TIMEOUT_SECONDS,
            "smoke_diagnostics_deadline_exceeded",
            monotonic,
        )
        try:
            diagnostics = project.diagnostic_states()
        except DockerProjectError:
            diagnostics = ({"service": "unavailable", "state": "unknown"},)
    finally:
        cleanup_error: BaseException | None = None
        cleanups = (
            (project.cleanup, _PROJECT_CLEANUP_TIMEOUT_SECONDS),
            (canary.cleanup, _CANARY_CLEANUP_TIMEOUT_SECONDS),
            (
                lambda: _cleanup_images(settings, smoke_command_runner),
                _IMAGE_CLEANUP_TIMEOUT_SECONDS,
            ),
        )
        for cleanup, timeout_seconds in cleanups:
            deadline = _Deadline.after(
                timeout_seconds,
                "smoke_cleanup_deadline_exceeded",
                monotonic,
            )
            try:
                cleanup()
            except (DockerProjectError, IsolatedSmokeError) as error:
                cleanup_error = cleanup_error or error
        if cleanup_error is not None:
            raise IsolatedSmokeError("smoke_cleanup_failed") from cleanup_error

    if primary_error is not None:
        movement_stage = _movement_stage(settings)
        if diagnostics or movement_stage:
            print(
                json.dumps(
                    {
                        "diagnostics": diagnostics,
                        "movement_stage": movement_stage or "unknown",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                file=sys.stderr,
            )
        raise primary_error


def _configure_grafana_public_url(
    settings: SmokeSettings,
    runner: Callable[[tuple[str, ...], float, str], str],
) -> None:
    """Recreate the API with the ephemeral browser URL assigned to Grafana."""
    published = runner(
        (*settings.compose, "port", "grafana", "3000"),
        30,
        "smoke_grafana_port_unavailable",
    ).strip()
    match = re.fullmatch(r"(?:127\.0\.0\.1|localhost|\[::1\]):([0-9]{1,5})", published)
    if match is None or not 1 <= int(match.group(1)) <= 65_535:
        raise IsolatedSmokeError("smoke_grafana_port_invalid")
    settings.environment["GRAFANA_BASE_URL"] = f"http://127.0.0.1:{match.group(1)}"
    runner(
        (
            *settings.compose,
            "up",
            "--detach",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "300",
            "api",
        ),
        300,
        "smoke_api_grafana_url_failed",
    )


def _movement_stage(settings: SmokeSettings) -> str | None:
    """Read the bounded, non-secret assertion marker written by smoke.sh."""
    candidate = settings.environment.get("SMOKE_PROGRESS_FILE")
    if candidate is None:
        return None
    try:
        value = Path(candidate).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if _MOVEMENT_STAGE_PATTERN.fullmatch(value) is None:
        return None
    return value


def main() -> int:
    """Run one isolated smoke scenario from command-line arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--scenario",
        choices=(
            "ingestion-relevance",
            "exact-release-missing",
            "duplicate-terminal-replay",
            "analysis-version-replay",
            "deterministic-finding-ui",
            "end-to-end-trace",
            "runtime-signals",
            "full-stack-drain",
            "worker-shutdown",
        ),
        default="ingestion-relevance",
    )
    arguments = parser.parse_args()
    try:
        with termination_signal_scope():
            with tempfile.TemporaryDirectory(prefix="pypi-fixture-smoke-") as temporary:
                settings = build_settings(
                    arguments.root,
                    Path(temporary),
                    scenario=arguments.scenario,
                )
                run(settings)
    except ProcessInterrupted as error:
        print("smoke_interrupted", file=sys.stderr)
        return 128 + error.signal_number
    except (DockerProjectError, IsolatedSmokeError, ProcessFailure) as error:
        print(error.code, file=sys.stderr)
        return 1
    print("Isolated fixture smoke passed.")
    return 0


def _validated_root(candidate: Path) -> Path:
    absolute = candidate.absolute()
    try:
        metadata = absolute.lstat()
        compose_metadata = (absolute / "docker-compose.yml").lstat()
        smoke_compose_metadata = (absolute / "infra" / "smoke.compose.yml").lstat()
    except OSError:
        raise IsolatedSmokeError("smoke_root_invalid") from None
    if (
        absolute.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or (absolute / "docker-compose.yml").is_symlink()
        or not stat.S_ISREG(compose_metadata.st_mode)
        or (absolute / "infra" / "smoke.compose.yml").is_symlink()
        or not stat.S_ISREG(smoke_compose_metadata.st_mode)
    ):
        raise IsolatedSmokeError("smoke_root_invalid")
    return absolute


def _resolve_compose_toolchain() -> tuple[Path, Path]:
    for candidate in _COMPOSE_EXECUTABLE_CANDIDATES:
        plugin_directory = candidate.parent
        buildx = plugin_directory / "docker-buildx"
        try:
            metadata = candidate.lstat()
            buildx_metadata = buildx.lstat()
            directory_metadata = plugin_directory.lstat()
        except OSError:
            continue
        if (
            candidate.is_symlink()
            or buildx.is_symlink()
            or plugin_directory.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or not stat.S_ISREG(buildx_metadata.st_mode)
            or not stat.S_ISDIR(directory_metadata.st_mode)
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or buildx_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or directory_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or not os.access(candidate, os.X_OK)
            or not os.access(buildx, os.X_OK)
        ):
            continue
        return candidate, plugin_directory
    raise IsolatedSmokeError("smoke_compose_toolchain_unavailable")


def _run_command(
    settings: SmokeSettings,
    command: tuple[str, ...],
    *,
    timeout_seconds: float,
    failure_code: str,
) -> str:
    try:
        return run_capture(
            command,
            settings.root,
            settings.environment,
            timeout_seconds=timeout_seconds,
            failure_code=failure_code,
        )
    except ProcessFailure as error:
        raise IsolatedSmokeError(error.code) from None


def _validate_model(settings: SmokeSettings, payload: str) -> None:
    try:
        model = json.loads(payload, object_pairs_hook=_unique_object)
        services = model["services"]
        networks = model["networks"]
        volumes = model["volumes"]
    except (json.JSONDecodeError, KeyError, TypeError, IsolatedSmokeError):
        raise IsolatedSmokeError("smoke_compose_model_invalid") from None
    if (
        not isinstance(services, dict)
        or not isinstance(networks, dict)
        or not isinstance(volumes, dict)
        or not services
    ):
        raise IsolatedSmokeError("smoke_compose_model_invalid")

    published_ports = [
        port
        for service in services.values()
        if isinstance(service, dict)
        for port in service.get("ports", [])
        if isinstance(port, dict)
    ]
    if len(published_ports) != 13 or any(
        port.get("host_ip") != "127.0.0.1" or port.get("published") not in {"", None}
        for port in published_ports
    ):
        raise IsolatedSmokeError("smoke_port_isolation_invalid")

    for service in services.values():
        if not isinstance(service, dict):
            raise IsolatedSmokeError("smoke_compose_model_invalid")
        labels = service.get("labels")
        if (
            not isinstance(labels, dict)
            or labels.get("io.pypi-change-intelligence.smoke-owner") != settings.owner
        ):
            raise IsolatedSmokeError("smoke_resource_labels_invalid")

    reasoning = services.get("reasoning-worker")
    source = services.get("connect-source")
    alloy = services.get("alloy")
    if (
        not isinstance(reasoning, dict)
        or not isinstance(source, dict)
        or not isinstance(alloy, dict)
        or reasoning.get("environment", {}).get("EVIDENCE_MODE")
        != settings.environment["EVIDENCE_MODE"]
        or reasoning.get("environment", {}).get("MODEL_MODE") != "fake"
        or reasoning.get("environment", {}).get("OPENAI_API_KEY") != ""
        or reasoning.get("environment", {}).get("OPENAI_PROMPT_CACHE_ENABLED") != "false"
        or reasoning.get("environment", {}).get("OBS_CAPTURE_MODEL_PAYLOADS") != "false"
        or alloy.get("environment", {}).get("PYPI_COMPOSE_PROJECT") != settings.project
        or source.get("environment", {}).get("SOURCE_FIXTURE_SCENARIO")
        != settings.environment["SOURCE_FIXTURE_SCENARIO"]
        or "source-fixture.yaml" not in json.dumps(source.get("command"))
    ):
        raise IsolatedSmokeError("smoke_safe_mode_invalid")

    for logical_name, network in networks.items():
        if (
            not isinstance(network, dict)
            or network.get("name") != f"{settings.project}_{logical_name}"
            or network.get("labels", {}).get("io.pypi-change-intelligence.smoke-owner")
            != settings.owner
            or network.get("ipam", {}).get("config")
        ):
            raise IsolatedSmokeError("smoke_network_isolation_invalid")
    for logical_name, volume in volumes.items():
        if (
            not isinstance(volume, dict)
            or volume.get("name") != f"{settings.project}_{logical_name}"
            or volume.get("labels", {}).get("io.pypi-change-intelligence.smoke-owner")
            != settings.owner
        ):
            raise IsolatedSmokeError("smoke_volume_isolation_invalid")

    observed_images = {
        service.get("image")
        for service in services.values()
        if isinstance(service, dict) and "build" in service
    }
    if observed_images != set(settings.image_tags):
        raise IsolatedSmokeError("smoke_image_isolation_invalid")


def _assert_images_absent(
    settings: SmokeSettings,
    run_command: Callable[[tuple[str, ...], float, str], str],
) -> None:
    for tag in settings.image_tags:
        if _image_tags(settings, tag, run_command):
            raise IsolatedSmokeError("smoke_image_collision")


def _start_canary(
    settings: SmokeSettings,
    canary: DockerProject,
    run_command: Callable[[tuple[str, ...], float, str], str],
) -> None:
    config_hash = hashlib.sha256(settings.canary_marker.encode()).hexdigest()
    output = run_command(
        (
            "docker",
            "--host",
            _DOCKER_ENDPOINT,
            "container",
            "create",
            "--label",
            f"com.docker.compose.project={settings.canary_project}",
            "--label",
            "com.docker.compose.service=canary",
            "--label",
            f"com.docker.compose.config-hash={config_hash}",
            "--label",
            f"io.pypi-change-intelligence.smoke-owner={settings.canary_owner}",
            _CANARY_IMAGE,
            "sh",
            "-c",
            'while :; do printf "%s\\n" "$1"; sleep 1; done',
            "sh",
            settings.canary_marker,
        ),
        30,
        "smoke_canary_create_failed",
    )
    container_ids = tuple(line for line in output.splitlines() if line)
    if len(container_ids) != 1 or _CONTAINER_ID_PATTERN.fullmatch(container_ids[0]) is None:
        raise IsolatedSmokeError("smoke_canary_identity_invalid")
    run_command(
        ("docker", "--host", _DOCKER_ENDPOINT, "container", "start", container_ids[0]),
        30,
        "smoke_canary_start_failed",
    )
    if canary.diagnostic_states() != ({"service": "canary", "state": "running"},):
        raise IsolatedSmokeError("smoke_canary_state_invalid")


def _verify_foreign_marker_absent(
    settings: SmokeSettings,
    run_command: Callable[[tuple[str, ...], float, str], str],
    deadline: _Deadline,
) -> None:
    published = run_command(
        (*settings.compose, "port", "loki", "3100"),
        30,
        "smoke_privacy_probe_failed",
    ).strip()
    match = re.fullmatch(r"127\.0\.0\.1:([1-9][0-9]{0,4})", published)
    if match is None or int(match.group(1)) > 65_535:
        raise IsolatedSmokeError("smoke_privacy_probe_failed")
    query = urllib.parse.urlencode(
        {
            "query": (
                f'{{compose_project="{settings.project}",service="canary"}} '
                f'|= "{settings.canary_marker}"'
            )
        }
    )
    url = f"http://127.0.0.1:{match.group(1)}/loki/api/v1/query_range?{query}"
    consecutive_empty = 0
    for _attempt in range(15):
        try:
            with urllib.request.urlopen(  # noqa: S310
                url,
                timeout=deadline.cap(5),
            ) as response:
                payload = response.read(262_145)
            if len(payload) > 262_144:
                raise IsolatedSmokeError("smoke_privacy_probe_failed")
            document = json.loads(payload, object_pairs_hook=_unique_object)
            results = document["data"]["result"]
            if document["status"] != "success" or not isinstance(results, list):
                raise IsolatedSmokeError("smoke_privacy_probe_failed")
        except (
            json.JSONDecodeError,
            KeyError,
            TypeError,
            urllib.error.URLError,
        ):
            consecutive_empty = 0
        else:
            if results:
                raise IsolatedSmokeError("smoke_docker_filter_leak")
            consecutive_empty += 1
            if consecutive_empty >= 5:
                return
        time.sleep(deadline.cap(2))
    raise IsolatedSmokeError("smoke_privacy_probe_failed")


def _cleanup_images(
    settings: SmokeSettings,
    run_command: Callable[[tuple[str, ...], float, str], str] | None = None,
) -> None:
    effective_runner = run_command
    if run_command is None:

        def default_runner(
            command: tuple[str, ...],
            timeout_seconds: float,
            failure_code: str,
        ) -> str:
            return _run_command(
                settings,
                command,
                timeout_seconds=timeout_seconds,
                failure_code=failure_code,
            )

        effective_runner = default_runner
    if effective_runner is None:
        raise IsolatedSmokeError("smoke_image_cleanup_failed")

    errors: list[IsolatedSmokeError] = []
    for tag in settings.image_tags:
        try:
            observed = _image_tags(settings, tag, effective_runner)
            if not observed:
                continue
            if observed != (tag,):
                raise IsolatedSmokeError("smoke_image_identity_invalid")
            effective_runner(
                ("docker", "--host", _DOCKER_ENDPOINT, "image", "rm", tag),
                120,
                "smoke_image_cleanup_failed",
            )
            if _image_tags(settings, tag, effective_runner):
                raise IsolatedSmokeError("smoke_image_cleanup_failed")
        except IsolatedSmokeError as error:
            errors.append(error)
    if errors:
        raise errors[0]


def _image_tags(
    settings: SmokeSettings,
    tag: str,
    run_command: Callable[[tuple[str, ...], float, str], str],
) -> tuple[str, ...]:
    if _IMAGE_PATTERN.fullmatch(tag) is None:
        raise IsolatedSmokeError("smoke_image_identity_invalid")
    output = run_command(
        (
            "docker",
            "--host",
            _DOCKER_ENDPOINT,
            "image",
            "ls",
            "--filter",
            f"reference={tag}",
            "--format",
            "{{.Repository}}:{{.Tag}}",
        ),
        30,
        "smoke_image_list_failed",
    )
    lines = tuple(line for line in output.splitlines() if line)
    if len(lines) != len(set(lines)) or any(line != tag for line in lines):
        raise IsolatedSmokeError("smoke_image_identity_invalid")
    return lines


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if not isinstance(key, str) or key in value:
            raise IsolatedSmokeError("smoke_json_duplicate_key")
        value[key] = item
    return value


if __name__ == "__main__":
    raise SystemExit(main())
