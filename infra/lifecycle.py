#!/usr/bin/env python3
"""Drain the local PyPI pipeline and stop it without deleting durable volumes."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard

CONSUMER_GROUPS = (
    "pypi-reasoning-v1",
    "pypi-postgres-sink-v1",
    "pypi-connect-trace-bridge-v1",
)

_DOCKER_ENDPOINT_FORMAT = '{{(index .Endpoints "docker").Host}}'
_PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_PROCESS_GROUP_GRACE_SECONDS = 0.2


class LifecycleError(RuntimeError):
    """A bounded, operator-actionable drain failure."""


@dataclass(frozen=True)
class StopSettings:
    """Settings for one ingress-first pipeline drain."""

    root: Path
    project_name: str
    max_polls: int
    poll_interval_seconds: float
    probe_timeout_seconds: float
    drain_timeout_seconds: float
    compose_executable: Path | None = None

    @property
    def compose(self) -> tuple[str, ...]:
        executable = (
            (str(self.compose_executable),)
            if self.compose_executable is not None
            else ("docker", "compose")
        )
        return (
            *executable,
            "--project-name",
            self.project_name,
            "--env-file",
            "/dev/null",
            "-f",
            str(self.root / "docker-compose.yml"),
            "--project-directory",
            str(self.root),
        )


def _run(
    command: tuple[str, ...],
    *,
    timeout_seconds: float,
    capture_stdout: bool = False,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str] | None:
    """Run one bounded command and terminate its process group on timeout."""
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            env=environment,
            start_new_session=True,
        )
    except OSError:
        return None
    try:
        stdout, _ = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        return None
    except BaseException:
        _terminate_process_group(process)
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout)


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=_PROCESS_GROUP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=_PROCESS_GROUP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _require_local_docker(probe_timeout_seconds: float) -> str:
    endpoint = os.environ.get("DOCKER_HOST", "")
    if not endpoint:
        result = _run(
            ("docker", "context", "inspect", "--format", _DOCKER_ENDPOINT_FORMAT),
            timeout_seconds=probe_timeout_seconds,
            capture_stdout=True,
        )
        if result is None or result.returncode != 0:
            raise LifecycleError("Docker context could not be inspected locally.")
        endpoint = result.stdout.strip()
    if not endpoint.startswith("unix:///") or any(character in endpoint for character in "\r\n"):
        raise LifecycleError("Select a local Unix-socket Docker context.")
    return endpoint


def _is_json_integer(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _partition_lag_is_zero(payload: str, expected_group: str) -> bool:
    """Accept only one complete, internally consistent zero-lag group report."""
    try:
        document: Any = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(document, list) or len(document) != 1:
        return False
    group = document[0]
    if not isinstance(group, dict) or "error" in group:
        return False
    if group.get("group_name") != expected_group:
        return False
    for field in ("coordinator_partition", "state", "balancer"):
        if not isinstance(group.get(field), str):
            return False
    members = group.get("members")
    total_lag = group.get("total_lag")
    if not _is_json_integer(members) or members < 0:
        return False
    if not _is_json_integer(group.get("coordinator_node")):
        return False
    if not _is_json_integer(total_lag) or total_lag < 0:
        return False
    member_details = group.get("members_details")
    if not isinstance(member_details, list) or len(member_details) != members:
        return False
    for member in member_details:
        if not isinstance(member, dict) or "error" in member:
            return False
        for field in ("member_id", "client_id", "host"):
            if not isinstance(member.get(field), str):
                return False
        if not isinstance(member.get("topic_partitions"), list):
            return False
    partitions = group.get("partitions")
    if not isinstance(partitions, list) or not partitions:
        return False

    identities: set[tuple[str, int]] = set()
    observed_total_lag = 0
    for partition in partitions:
        if not isinstance(partition, dict) or "error" in partition:
            return False
        topic = partition.get("topic")
        partition_id = partition.get("partition")
        if not isinstance(topic, str) or not topic or not _is_json_integer(partition_id):
            return False
        identity = (topic, partition_id)
        if partition_id < 0 or identity in identities:
            return False
        identities.add(identity)
        # rpk uses -1 when a group has no committed offset on an untouched empty partition.
        current_offset = partition.get("current_offset")
        if not _is_json_integer(current_offset) or current_offset < -1:
            return False
        for field in ("log_start_offset", "log_end_offset", "lag"):
            value = partition.get(field)
            if not _is_json_integer(value) or value < 0:
                return False
        if current_offset == -1 and any(
            partition[field] != 0 for field in ("log_start_offset", "log_end_offset", "lag")
        ):
            return False
        for field in ("member_id", "client_id", "host"):
            if not isinstance(partition.get(field), str):
                return False
        observed_total_lag += partition["lag"]

    return total_lag == observed_total_lag == 0


def _docker_environment(probe_timeout_seconds: float) -> dict[str, str]:
    environment = os.environ.copy()
    environment["DOCKER_HOST"] = _require_local_docker(probe_timeout_seconds)
    environment.pop("DOCKER_CONTEXT", None)
    return environment


def _group_is_drained(
    settings: StopSettings,
    group: str,
    *,
    deadline: float,
    docker_environment: Mapping[str, str],
) -> bool:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    result = _run(
        (
            *settings.compose,
            "exec",
            "-T",
            "redpanda",
            "rpk",
            "group",
            "describe",
            group,
            "-X",
            "brokers=redpanda:9092",
            "--format",
            "json",
        ),
        timeout_seconds=min(settings.probe_timeout_seconds, remaining),
        capture_stdout=True,
        environment=docker_environment,
    )
    return (
        result is not None
        and result.returncode == 0
        and _partition_lag_is_zero(result.stdout, group)
    )


def _drain_ingress(
    settings: StopSettings,
    *,
    docker_environment: Mapping[str, str],
    announce: bool,
) -> str:
    project_state = _run(
        (*settings.compose, "ps", "-a", "-q"),
        timeout_seconds=settings.probe_timeout_seconds,
        capture_stdout=True,
        environment=docker_environment,
    )
    if project_state is None or project_state.returncode != 0:
        raise LifecycleError("Compose project state could not be inspected.")
    if not project_state.stdout.strip():
        if announce:
            print("Lifecycle project is already stopped; no ingress was changed.")
        return "already_stopped"

    source_stop = _run(
        (*settings.compose, "stop", "--timeout", "60", "connect-source"),
        timeout_seconds=120,
        environment=docker_environment,
    )
    if source_stop is None or source_stop.returncode != 0:
        raise LifecycleError(
            "Ingress stop did not complete; source state is unknown, so inspect project state before retrying."
        )

    deadline = time.monotonic() + settings.drain_timeout_seconds
    consecutive_drained_samples = 0
    for poll_index in range(settings.max_polls):
        sample_is_drained = True
        for group in CONSUMER_GROUPS:
            if not _group_is_drained(
                settings,
                group,
                deadline=deadline,
                docker_environment=docker_environment,
            ):
                sample_is_drained = False
        consecutive_drained_samples = consecutive_drained_samples + 1 if sample_is_drained else 0
        if consecutive_drained_samples >= 2:
            break
        if poll_index + 1 < settings.max_polls:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(settings.poll_interval_seconds, remaining))
    if consecutive_drained_samples < 2:
        raise LifecycleError("Consumer lag did not drain; ingress remains stopped for inspection.")
    if announce:
        print("Lifecycle drain completed; ingress is stopped and services remain running.")
    return "drain_proven"


def drain_ingress_safely(settings: StopSettings) -> str:
    """Stop ingress and prove downstream consumer groups are drained."""
    _validate_settings(settings)
    return _drain_ingress(
        settings,
        docker_environment=_docker_environment(settings.probe_timeout_seconds),
        announce=True,
    )


def stop_safely(settings: StopSettings) -> str:
    """Drain the pipeline and stop Compose without deleting volumes."""
    _validate_settings(settings)
    docker_environment = _docker_environment(settings.probe_timeout_seconds)
    drain_state = _drain_ingress(settings, docker_environment=docker_environment, announce=False)
    if drain_state == "already_stopped":
        print("Lifecycle project is already stopped; durable volumes were not changed.")
        return drain_state
    shutdown = _run(
        (*settings.compose, "down", "--timeout", "60", "--remove-orphans"),
        timeout_seconds=120,
        environment=docker_environment,
    )
    if shutdown is None or shutdown.returncode != 0:
        raise LifecycleError(
            "Shutdown failed after drain; ingress remains stopped and volumes are intact."
        )
    print("Lifecycle stop completed; durable volumes were preserved.")
    return "drain_proven"


def _validate_settings(settings: StopSettings) -> None:
    if _PROJECT_NAME.fullmatch(settings.project_name) is None:
        raise LifecycleError("Compose project name is invalid.")
    if not (settings.root / "docker-compose.yml").is_file():
        raise LifecycleError("Checked-in Compose configuration is unavailable.")
    if settings.compose_executable is not None and (
        not settings.compose_executable.is_absolute() or not settings.compose_executable.is_file()
    ):
        raise LifecycleError("Compose executable must be an absolute existing file.")
    if not 1 <= settings.max_polls <= 1_000:
        raise LifecycleError("Maximum drain polls must be between 1 and 1000.")
    if not 0 <= settings.poll_interval_seconds <= 60:
        raise LifecycleError("Drain poll interval must be between 0 and 60 seconds.")
    if not 0 < settings.probe_timeout_seconds <= 60:
        raise LifecycleError("Probe timeout must be greater than 0 and at most 60 seconds.")
    if not 0 < settings.drain_timeout_seconds <= 3_600:
        raise LifecycleError("Drain timeout must be greater than 0 and at most 3600 seconds.")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--compose-executable", type=Path)
    parser.add_argument(
        "--project-name",
        default=os.environ.get("LIFECYCLE_PROJECT", "pypi-change-intelligence"),
    )
    parser.add_argument("--max-polls", type=int, default=30)
    parser.add_argument("--poll-interval-seconds", type=float, default=2)
    parser.add_argument("--probe-timeout-seconds", type=float, default=5)
    parser.add_argument("--drain-timeout-seconds", type=float, default=120)
    parser.add_argument("command", choices=("drain", "stop"))
    return parser.parse_args()


def main() -> int:
    """Run the requested drain or safe-stop lifecycle operation."""
    args = _arguments()
    settings = StopSettings(
        root=args.root.resolve(),
        project_name=args.project_name,
        max_polls=args.max_polls,
        poll_interval_seconds=args.poll_interval_seconds,
        probe_timeout_seconds=args.probe_timeout_seconds,
        drain_timeout_seconds=args.drain_timeout_seconds,
        compose_executable=args.compose_executable,
    )
    try:
        if args.command == "drain":
            drain_ingress_safely(settings)
        else:
            stop_safely(settings)
    except LifecycleError as error:
        print(f"Lifecycle {args.command} failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            f"Lifecycle {args.command} interrupted; inspect project state before retrying.",
            file=sys.stderr,
        )
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
