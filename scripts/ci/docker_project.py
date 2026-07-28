"""Ownership-safe cleanup for one disposable Docker Compose project."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_NETWORK_ID = re.compile(r"[0-9a-f]{64}")
_PROJECT = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_OWNER = re.compile(r"[0-9a-f]{32}")
_LOGICAL_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_ABSENCE_STABILITY_SECONDS = 0.2
_NETWORK_RESERVATION_ATTEMPTS = 16
_NETWORK_SPECS = (
    ("pipeline", 27, False),
    ("docker-observability", 29, True),
)

CommandRunner = Callable[[tuple[str, ...], float, str], str]


class DockerProjectError(RuntimeError):
    """A stable project-isolation failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ContainerIdentity:
    """Immutable container ID plus verified Compose ownership labels."""

    container_id: str
    service: str
    config_hash: str


@dataclass(frozen=True)
class NetworkIdentity:
    """Immutable network ID plus verified Compose ownership labels."""

    network_id: str
    logical_name: str


@dataclass(frozen=True)
class VolumeIdentity:
    """The strongest Docker volume identity available before exact-name removal."""

    name: str
    logical_name: str
    created_at: str
    driver: str
    options: dict[str, str]


class DockerProject:
    """Inspect and remove only resources owned by one unguessable project."""

    def __init__(
        self,
        *,
        endpoint: str,
        project: str,
        owner: str,
        run: CommandRunner,
    ) -> None:
        if endpoint != "unix:///var/run/docker.sock":
            raise DockerProjectError("docker_endpoint_invalid")
        if _PROJECT.fullmatch(project) is None or _OWNER.fullmatch(owner) is None:
            raise DockerProjectError("docker_project_identity_invalid")
        self._endpoint = endpoint
        self._project = project
        self._owner = owner
        self._run = run

    def assert_absent(self) -> None:
        """Reject a preexisting resource that carries this project label."""
        if self._container_ids() or self._network_ids() or self._volume_names():
            raise DockerProjectError("docker_project_collision")

    def reserve_networks(self) -> None:
        """Atomically reserve explicit project networks outside Docker's default pool."""
        if self._network_ids():
            raise DockerProjectError("docker_project_collision")
        for logical_name, prefix_length, internal in _NETWORK_SPECS:
            self._reserve_network(logical_name, prefix_length, internal)

    def cleanup(self) -> None:
        """Remove verified resources in dependency order and prove stable absence."""
        errors: list[DockerProjectError] = []
        self._cleanup_containers(errors)
        self._cleanup_networks(errors)
        self._cleanup_volumes(errors)
        self._verify_stable_absence(errors)
        if errors:
            raise errors[0]

    def _cleanup_containers(self, errors: list[DockerProjectError]) -> None:
        try:
            container_ids = self._container_ids()
        except DockerProjectError as error:
            errors.append(error)
            return
        for container_id in container_ids:
            try:
                identity = self._inspect_container(container_id)
                if self._inspect_container(identity.container_id) != identity:
                    raise DockerProjectError("docker_container_identity_changed")
                self._docker(
                    "container",
                    "rm",
                    "--force",
                    identity.container_id,
                    failure_code="docker_container_cleanup_failed",
                )
                if identity.container_id in self._container_ids(id_filter=identity.container_id):
                    raise DockerProjectError("docker_container_cleanup_failed")
            except DockerProjectError as error:
                errors.append(error)

    def _cleanup_networks(self, errors: list[DockerProjectError]) -> None:
        try:
            network_ids = self._network_ids()
        except DockerProjectError as error:
            errors.append(error)
            return
        for network_id in network_ids:
            try:
                identity = self._inspect_network(network_id)
                if self._inspect_network(identity.network_id) != identity:
                    raise DockerProjectError("docker_network_identity_changed")
                self._docker(
                    "network",
                    "rm",
                    identity.network_id,
                    failure_code="docker_network_cleanup_failed",
                )
                if identity.network_id in self._network_ids(id_filter=identity.network_id):
                    raise DockerProjectError("docker_network_cleanup_failed")
            except DockerProjectError as error:
                errors.append(error)

    def _cleanup_volumes(self, errors: list[DockerProjectError]) -> None:
        try:
            volume_names = self._volume_names()
        except DockerProjectError as error:
            errors.append(error)
            return
        for name in volume_names:
            try:
                identity = self._inspect_volume(name)
                if self._volume_users(identity.name):
                    raise DockerProjectError("docker_volume_in_use")
                if self._inspect_volume(identity.name) != identity:
                    raise DockerProjectError("docker_volume_identity_changed")
                self._docker(
                    "volume",
                    "rm",
                    identity.name,
                    failure_code="docker_volume_cleanup_failed",
                )
                if identity.name in self._volume_names(name_filter=identity.name):
                    raise DockerProjectError("docker_volume_cleanup_failed")
            except DockerProjectError as error:
                errors.append(error)

    def _verify_stable_absence(self, errors: list[DockerProjectError]) -> None:
        try:
            quiet_since = time.monotonic()
            while time.monotonic() - quiet_since < _ABSENCE_STABILITY_SECONDS:
                if self._container_ids() or self._network_ids() or self._volume_names():
                    raise DockerProjectError("docker_project_cleanup_incomplete")
                time.sleep(0.02)
        except DockerProjectError as error:
            errors.append(error)

    def diagnostic_states(self) -> tuple[dict[str, str], ...]:
        """Return bounded allowlisted state for owned containers only."""
        states: list[dict[str, str]] = []
        for identity in self._containers():
            state = self._inspect_value(
                "container",
                identity.container_id,
                "{{.State.Status}}",
                "docker_diagnostics_failed",
            )
            if state not in {
                "created",
                "running",
                "paused",
                "restarting",
                "removing",
                "exited",
                "dead",
            }:
                state = "unknown"
            states.append({"service": identity.service, "state": state})
        return tuple(sorted(states, key=lambda item: item["service"]))

    def _containers(self) -> tuple[ContainerIdentity, ...]:
        return tuple(
            self._inspect_container(container_id) for container_id in self._container_ids()
        )

    def _networks(self) -> tuple[NetworkIdentity, ...]:
        return tuple(self._inspect_network(network_id) for network_id in self._network_ids())

    def _volumes(self) -> tuple[VolumeIdentity, ...]:
        return tuple(self._inspect_volume(name) for name in self._volume_names())

    def _container_ids(self, *, id_filter: str | None = None) -> tuple[str, ...]:
        command = [
            "container",
            "ls",
            "--all",
            "--no-trunc",
            "--filter",
            self._project_filter,
        ]
        if id_filter is not None:
            if _CONTAINER_ID.fullmatch(id_filter) is None:
                raise DockerProjectError("docker_container_identity_invalid")
            command.extend(("--filter", f"id={id_filter}"))
        command.extend(("--format", "{{.ID}}"))
        return self._validated_lines(
            self._docker(*command, failure_code="docker_container_list_failed"),
            _CONTAINER_ID,
            "docker_container_list_invalid",
        )

    def _network_ids(self, *, id_filter: str | None = None) -> tuple[str, ...]:
        command = ["network", "ls", "--no-trunc", "--filter", self._project_filter]
        if id_filter is not None:
            if _NETWORK_ID.fullmatch(id_filter) is None:
                raise DockerProjectError("docker_network_identity_invalid")
            command.extend(("--filter", f"id={id_filter}"))
        command.extend(("--format", "{{.ID}}"))
        return self._validated_lines(
            self._docker(*command, failure_code="docker_network_list_failed"),
            _NETWORK_ID,
            "docker_network_list_invalid",
        )

    def _volume_names(self, *, name_filter: str | None = None) -> tuple[str, ...]:
        command = ["volume", "ls", "--filter", self._project_filter]
        if name_filter is not None:
            if not name_filter.startswith(f"{self._project}_"):
                raise DockerProjectError("docker_volume_identity_invalid")
            command.extend(("--filter", f"name=^{re.escape(name_filter)}$"))
        command.extend(("--format", "{{.Name}}"))
        return self._validated_volume_names(
            self._docker(*command, failure_code="docker_volume_list_failed")
        )

    def _inspect_container(self, container_id: str) -> ContainerIdentity:
        if _CONTAINER_ID.fullmatch(container_id) is None:
            raise DockerProjectError("docker_container_identity_invalid")
        observed_id = self._inspect_value(
            "container",
            container_id,
            "{{.Id}}",
            "docker_container_inspect_failed",
        )
        if observed_id != container_id:
            raise DockerProjectError("docker_container_identity_invalid")
        labels = self._inspect_labels("container", container_id)
        self._validate_common_labels(labels)
        service = labels.get("com.docker.compose.service", "")
        config_hash = labels.get("com.docker.compose.config-hash", "")
        if (
            _LOGICAL_NAME.fullmatch(service) is None
            or re.fullmatch(r"[0-9a-f]{64}", config_hash) is None
        ):
            raise DockerProjectError("docker_container_identity_invalid")
        return ContainerIdentity(container_id, service, config_hash)

    def _inspect_network(self, network_id: str) -> NetworkIdentity:
        if _NETWORK_ID.fullmatch(network_id) is None:
            raise DockerProjectError("docker_network_identity_invalid")
        observed_id = self._inspect_value(
            "network",
            network_id,
            "{{.Id}}",
            "docker_network_inspect_failed",
        )
        if observed_id != network_id:
            raise DockerProjectError("docker_network_identity_invalid")
        labels = self._inspect_labels("network", network_id)
        self._validate_common_labels(labels)
        logical_name = labels.get("com.docker.compose.network", "")
        if _LOGICAL_NAME.fullmatch(logical_name) is None:
            raise DockerProjectError("docker_network_identity_invalid")
        return NetworkIdentity(network_id, logical_name)

    def _reserve_network(
        self,
        logical_name: str,
        prefix_length: int,
        internal: bool,
    ) -> None:
        name = f"{self._project}_{logical_name}"
        for attempt in range(_NETWORK_RESERVATION_ATTEMPTS):
            command = [
                "network",
                "create",
                "--driver",
                "bridge",
                "--subnet",
                self._network_subnet(logical_name, prefix_length, attempt),
                "--label",
                f"com.docker.compose.project={self._project}",
                "--label",
                f"com.docker.compose.network={logical_name}",
                "--label",
                f"io.pypi-change-intelligence.smoke-owner={self._owner}",
            ]
            if internal:
                command.append("--internal")
            command.append(name)
            try:
                output = self._docker(
                    *command,
                    failure_code="docker_network_reservation_retryable",
                )
            except DockerProjectError as error:
                if error.code != "docker_network_reservation_retryable":
                    raise
                continue
            network_ids = self._validated_lines(
                output,
                _NETWORK_ID,
                "docker_network_reservation_invalid",
            )
            if len(network_ids) != 1:
                raise DockerProjectError("docker_network_reservation_invalid")
            identity = self._inspect_network(network_ids[0])
            observed_name = self._inspect_value(
                "network",
                identity.network_id,
                "{{.Name}}",
                "docker_network_reservation_invalid",
            )
            if identity.logical_name != logical_name or observed_name != name:
                raise DockerProjectError("docker_network_reservation_invalid")
            return
        raise DockerProjectError("docker_network_reservation_failed")

    def _network_subnet(
        self,
        logical_name: str,
        prefix_length: int,
        attempt: int,
    ) -> str:
        # RFC 2544's 198.18.0.0/15 benchmark range is non-routable. Deriving a
        # candidate from the cryptographic owner keeps concurrent projects
        # independent; Docker network creation is the atomic collision arbiter.
        digest = hashlib.sha256(
            f"{self._project}\0{self._owner}\0{logical_name}\0{attempt}".encode()
        ).digest()
        base_prefix = 15
        block_count = 1 << (prefix_length - base_prefix)
        block_size = 1 << (32 - prefix_length)
        block = int.from_bytes(digest[:4], byteorder="big") % block_count
        address = ipaddress.IPv4Address(
            int(ipaddress.IPv4Address("198.18.0.0")) + block * block_size
        )
        return f"{address}/{prefix_length}"

    def _inspect_volume(self, name: str) -> VolumeIdentity:
        if not name.startswith(f"{self._project}_"):
            raise DockerProjectError("docker_volume_identity_invalid")
        observed_name = self._inspect_value(
            "volume",
            name,
            "{{.Name}}",
            "docker_volume_inspect_failed",
        )
        if observed_name != name:
            raise DockerProjectError("docker_volume_identity_invalid")
        labels = self._inspect_labels("volume", name)
        self._validate_common_labels(labels)
        logical_name = labels.get("com.docker.compose.volume", "")
        created_at = self._inspect_value(
            "volume",
            name,
            "{{.CreatedAt}}",
            "docker_volume_inspect_failed",
        )
        driver = self._inspect_value(
            "volume",
            name,
            "{{.Driver}}",
            "docker_volume_inspect_failed",
        )
        options = self._inspect_json_object(
            "volume",
            name,
            "{{json .Options}}",
            "docker_volume_inspect_failed",
        )
        if (
            _LOGICAL_NAME.fullmatch(logical_name) is None
            or not created_at
            or not driver
            or not all(isinstance(value, str) for value in options.values())
        ):
            raise DockerProjectError("docker_volume_identity_invalid")
        typed_options = {key: value for key, value in options.items() if isinstance(value, str)}
        return VolumeIdentity(name, logical_name, created_at, driver, typed_options)

    def _volume_users(self, name: str) -> tuple[str, ...]:
        return self._validated_lines(
            self._docker(
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--filter",
                f"volume={name}",
                "--format",
                "{{.ID}}",
                failure_code="docker_volume_users_failed",
            ),
            _CONTAINER_ID,
            "docker_volume_users_invalid",
        )

    def _inspect_labels(self, kind: str, identity: str) -> dict[str, str]:
        labels = self._inspect_json_object(
            kind,
            identity,
            "{{json .Labels}}" if kind != "container" else "{{json .Config.Labels}}",
            f"docker_{kind}_inspect_failed",
        )
        if not all(isinstance(value, str) for value in labels.values()):
            raise DockerProjectError(f"docker_{kind}_identity_invalid")
        return {key: value for key, value in labels.items() if isinstance(value, str)}

    def _inspect_json_object(
        self,
        kind: str,
        identity: str,
        template: str,
        failure_code: str,
    ) -> dict[str, object]:
        payload = self._inspect_value(kind, identity, template, failure_code)
        try:
            value = json.loads(payload, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, DockerProjectError):
            raise DockerProjectError(failure_code) from None
        # Docker serializes the default local-volume options as JSON null.
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise DockerProjectError(failure_code)
        return value

    def _inspect_value(
        self,
        kind: str,
        identity: str,
        template: str,
        failure_code: str,
    ) -> str:
        output = self._docker(
            kind,
            "inspect",
            "--format",
            template,
            identity,
            failure_code=failure_code,
        )
        value = output.strip()
        if not value or "\n" in value:
            raise DockerProjectError(failure_code)
        return value

    def _validate_common_labels(self, labels: dict[str, str]) -> None:
        if (
            labels.get("com.docker.compose.project") != self._project
            or labels.get("io.pypi-change-intelligence.smoke-owner") != self._owner
        ):
            raise DockerProjectError("docker_resource_ownership_invalid")

    @property
    def _project_filter(self) -> str:
        return f"label=com.docker.compose.project={self._project}"

    def _docker(self, *arguments: str, failure_code: str) -> str:
        try:
            return self._run(
                ("docker", "--host", self._endpoint, *arguments),
                30,
                failure_code,
            )
        except DockerProjectError:
            raise
        except Exception:
            raise DockerProjectError(failure_code) from None

    @staticmethod
    def _validated_lines(
        output: str,
        pattern: re.Pattern[str],
        error_code: str,
    ) -> tuple[str, ...]:
        lines = tuple(line for line in output.splitlines() if line)
        if len(lines) != len(set(lines)) or any(pattern.fullmatch(line) is None for line in lines):
            raise DockerProjectError(error_code)
        return tuple(sorted(lines))

    def _validated_volume_names(self, output: str) -> tuple[str, ...]:
        lines = tuple(line for line in output.splitlines() if line)
        if len(lines) != len(set(lines)) or any(
            not line.startswith(f"{self._project}_")
            or _LOGICAL_NAME.fullmatch(line.removeprefix(f"{self._project}_")) is None
            for line in lines
        ):
            raise DockerProjectError("docker_volume_list_invalid")
        return tuple(sorted(lines))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if not isinstance(key, str) or key in value:
            raise DockerProjectError("docker_inspect_json_invalid")
        value[key] = item
    return value
