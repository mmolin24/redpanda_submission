from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from scripts.ci.docker_project import DockerProject, DockerProjectError
from scripts.ci.isolated_smoke import (
    IsolatedSmokeError,
    _cleanup_images,
    _configure_grafana_public_url,
    _movement_stage,
    _validate_model,
    build_settings,
    run,
)
from scripts.ci.process import ProcessInterrupted

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "pypi-fixture-smoke-" + ("a" * 32)
OWNER = "b" * 32
CONTAINER_ID = "c" * 64
NETWORK_ID = "d" * 64
OBS_NETWORK_ID = "8" * 64
FOREIGN_CONTAINER_ID = "e" * 64
VOLUME_NAME = f"{PROJECT}_postgres-data"


class _FakeDockerDaemon:
    def __init__(
        self,
        *,
        drift_container: bool = False,
        owned_resources: bool = True,
        network_create_failures: int = 0,
    ) -> None:
        self.drift_container = drift_container
        self.container_label_reads = 0
        self.network_create_failures = network_create_failures
        self.network_create_commands: list[tuple[str, ...]] = []
        self.containers: dict[str, dict[str, Any]] = {
            CONTAINER_ID: {
                "labels": {
                    "com.docker.compose.project": PROJECT,
                    "com.docker.compose.service": "postgres",
                    "com.docker.compose.config-hash": "1" * 64,
                    "io.pypi-change-intelligence.smoke-owner": OWNER,
                },
                "state": "running",
            },
            FOREIGN_CONTAINER_ID: {
                "labels": {
                    "com.docker.compose.project": "foreign-project",
                    "com.docker.compose.service": "canary",
                    "com.docker.compose.config-hash": "2" * 64,
                    "io.pypi-change-intelligence.smoke-owner": "f" * 32,
                },
                "state": "running",
            },
        }
        if not owned_resources:
            self.containers.pop(CONTAINER_ID)
        self.networks = {
            NETWORK_ID: {
                "name": f"{PROJECT}_pipeline",
                "labels": {
                    "com.docker.compose.project": PROJECT,
                    "com.docker.compose.network": "pipeline",
                    "io.pypi-change-intelligence.smoke-owner": OWNER,
                },
            }
        }
        if not owned_resources:
            self.networks.clear()
        self.volumes = {
            VOLUME_NAME: {
                "labels": {
                    "com.docker.compose.project": PROJECT,
                    "com.docker.compose.volume": "postgres-data",
                    "io.pypi-change-intelligence.smoke-owner": OWNER,
                },
                "created_at": "2026-07-23T22:00:00Z",
                "driver": "local",
                # Docker reports absent local-volume options as JSON null.
                "options": None,
            }
        }
        if not owned_resources:
            self.volumes.clear()
        self.removals: list[tuple[str, str]] = []

    def run(self, command: tuple[str, ...], _timeout: float, _code: str) -> str:
        self._assert_equal_endpoint(command)
        arguments = command[3:]
        kind = arguments[0]
        action = arguments[1]
        if (kind, action) == ("container", "ls"):
            if any(value.startswith("volume=") for value in arguments):
                return ""
            ids = [
                container_id
                for container_id, value in self.containers.items()
                if value["labels"].get("com.docker.compose.project") == PROJECT
            ]
            requested = next(
                (value.removeprefix("id=") for value in arguments if value.startswith("id=")),
                None,
            )
            if requested is not None:
                ids = [container_id for container_id in ids if container_id == requested]
            return "".join(f"{container_id}\n" for container_id in ids)
        if (kind, action) == ("network", "ls"):
            ids = list(self.networks)
            requested = next(
                (value.removeprefix("id=") for value in arguments if value.startswith("id=")),
                None,
            )
            if requested is not None:
                ids = [network_id for network_id in ids if network_id == requested]
            return "".join(f"{network_id}\n" for network_id in ids)
        if (kind, action) == ("network", "create"):
            self.network_create_commands.append(arguments)
            if self.network_create_failures:
                self.network_create_failures -= 1
                raise DockerProjectError(_code)
            logical_name = next(
                value.removeprefix("com.docker.compose.network=")
                for value in arguments
                if value.startswith("com.docker.compose.network=")
            )
            identity = NETWORK_ID if logical_name == "pipeline" else OBS_NETWORK_ID
            labels = {
                value.split("=", 1)[0]: value.split("=", 1)[1]
                for index, value in enumerate(arguments)
                if index > 0 and arguments[index - 1] == "--label"
            }
            self.networks[identity] = {
                "name": arguments[-1],
                "labels": labels,
            }
            return f"{identity}\n"
        if (kind, action) == ("volume", "ls"):
            return "".join(f"{name}\n" for name in self.volumes)
        if action == "inspect":
            template = arguments[arguments.index("--format") + 1]
            identity = arguments[-1]
            return self._inspect(kind, identity, template)
        if action == "rm":
            identity = arguments[-1]
            if kind == "container":
                self.containers.pop(identity)
            elif kind == "network":
                self.networks.pop(identity)
            else:
                self.volumes.pop(identity)
            self.removals.append((kind, identity))
            return f"{identity}\n"
        raise AssertionError(arguments)

    @staticmethod
    def _assert_equal_endpoint(command: tuple[str, ...]) -> None:
        if command[:3] != ("docker", "--host", "unix:///var/run/docker.sock"):
            raise AssertionError(command)

    def _inspect(self, kind: str, identity: str, template: str) -> str:
        if kind == "container":
            value = self.containers[identity]
            if template == "{{.Id}}":
                return f"{identity}\n"
            if template == "{{json .Config.Labels}}":
                self.container_label_reads += 1
                labels = dict(value["labels"])
                if self.drift_container and self.container_label_reads > 1:
                    labels["com.docker.compose.config-hash"] = "9" * 64
                return json.dumps(labels) + "\n"
            if template == "{{.State.Status}}":
                return f"{value['state']}\n"
        if kind == "network":
            value = self.networks[identity]
            if template == "{{.Id}}":
                return f"{identity}\n"
            if template == "{{.Name}}":
                return f"{value['name']}\n"
            if template == "{{json .Labels}}":
                return json.dumps(value["labels"]) + "\n"
        if kind == "volume":
            value = self.volumes[identity]
            values = {
                "{{.Name}}": identity,
                "{{json .Labels}}": json.dumps(value["labels"]),
                "{{.CreatedAt}}": value["created_at"],
                "{{.Driver}}": value["driver"],
                "{{json .Options}}": json.dumps(value["options"]),
            }
            if template in values:
                return f"{values[template]}\n"
        raise AssertionError((kind, identity, template))


class IsolatedSmokeTests(unittest.TestCase):
    def test_settings_and_compose_model_force_complete_isolation(self) -> None:
        tokens = iter(("a" * 32, "b" * 32))
        with tempfile.TemporaryDirectory() as directory:
            settings = build_settings(
                ROOT,
                Path(directory),
                token_hex=lambda _bytes: next(tokens),
                parent_environment={
                    "PATH": os.environ["PATH"],
                    "OPENAI_API_KEY": "must-not-survive",
                    "DOCKER_CONTEXT": "foreign",
                    "COMPOSE_FILE": "/tmp/foreign.yml",
                },
            )
            result = subprocess.run(
                (*settings.compose, "config", "--format", "json"),
                cwd=ROOT,
                env=settings.environment,
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(settings.project, PROJECT)
            self.assertEqual(settings.owner, OWNER)
            self.assertEqual(settings.environment["OPENAI_API_KEY"], "")
            self.assertEqual(settings.environment["EVIDENCE_MODE"], "fixture")
            self.assertEqual(settings.environment["MODEL_MODE"], "fake")
            self.assertEqual(settings.environment["SOURCE_CONFIG"], "source-fixture.yaml")
            self.assertEqual(
                settings.environment["SOURCE_FIXTURE_SCENARIO"],
                "ingestion-relevance",
            )
            self.assertNotIn("DOCKER_CONTEXT", settings.environment)
            self.assertNotIn("COMPOSE_FILE", settings.environment)
            self.assertRegex(
                settings.environment["SMOKE_FOREIGN_MARKER"],
                r"^foreign-marker-[0-9a-f]{32}$",
            )
            self.assertEqual(
                settings.environment["SMOKE_COMPOSE_EXECUTABLE"],
                str(settings.compose_executable),
            )
            self.assertEqual(
                settings.environment["SMOKE_PROGRESS_FILE"],
                str(Path(directory) / "movement-stage"),
            )
            self.assertEqual(
                settings.environment["SMOKE_COMPOSE_OVERRIDE_FILE"],
                str(ROOT / "infra" / "smoke.compose.yml"),
            )
            self.assertTrue(settings.compose_executable.is_absolute())
            docker_configuration = json.loads(
                (Path(settings.environment["DOCKER_CONFIG"]) / "config.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                docker_configuration,
                {
                    "cliPluginsExtraDirs": [
                        str(settings.compose_executable.parent),
                    ]
                },
            )
            self.assertEqual(
                {
                    name
                    for name, value in settings.environment.items()
                    if name.endswith("_PORT") and value == ""
                },
                {
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
                },
            )
            _validate_model(settings, result.stdout)

    def test_exact_release_missing_settings_keep_fake_model_with_pypi_evidence(
        self,
    ) -> None:
        tokens = iter(("a" * 32, "b" * 32))
        with tempfile.TemporaryDirectory() as directory:
            settings = build_settings(
                ROOT,
                Path(directory),
                scenario="exact-release-missing",
                token_hex=lambda _bytes: next(tokens),
                parent_environment={
                    "PATH": os.environ["PATH"],
                    "OPENAI_API_KEY": "must-not-survive",
                },
            )
            result = subprocess.run(
                (*settings.compose, "config", "--format", "json"),
                cwd=ROOT,
                env=settings.environment,
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(settings.environment["MODEL_MODE"], "fake")
            self.assertEqual(settings.environment["OPENAI_API_KEY"], "")
            self.assertEqual(settings.environment["EVIDENCE_MODE"], "pypi")
            self.assertEqual(
                settings.environment["SOURCE_FIXTURE_SCENARIO"],
                "exact-release-missing",
            )
            _validate_model(settings, result.stdout)

    def test_duplicate_terminal_replay_uses_the_offline_fixture_stack(self) -> None:
        tokens = iter(("a" * 32, "b" * 32))
        with tempfile.TemporaryDirectory() as directory:
            settings = build_settings(
                ROOT,
                Path(directory),
                scenario="duplicate-terminal-replay",
                token_hex=lambda _bytes: next(tokens),
                parent_environment={
                    "PATH": os.environ["PATH"],
                    "OPENAI_API_KEY": "secret",
                },
            )
            result = subprocess.run(
                (*settings.compose, "config", "--format", "json"),
                cwd=ROOT,
                env=settings.environment,
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(settings.environment["SMOKE_SCENARIO"], "duplicate-terminal-replay")
            self.assertEqual(settings.environment["SOURCE_FIXTURE_SCENARIO"], "ingestion-relevance")
            self.assertEqual(settings.environment["EVIDENCE_MODE"], "fixture")
            self.assertEqual(settings.environment["MODEL_MODE"], "fake")
            self.assertEqual(settings.environment["OPENAI_API_KEY"], "")
            _validate_model(settings, result.stdout)

    def test_analysis_version_replay_starts_with_a_bounded_policy_revision(
        self,
    ) -> None:
        tokens = iter(("a" * 32, "b" * 32))
        with tempfile.TemporaryDirectory() as directory:
            settings = build_settings(
                ROOT,
                Path(directory),
                scenario="analysis-version-replay",
                token_hex=lambda _bytes: next(tokens),
                parent_environment={
                    "PATH": os.environ["PATH"],
                    "OPENAI_API_KEY": "secret",
                },
            )
            result = subprocess.run(
                (*settings.compose, "config", "--format", "json"),
                cwd=ROOT,
                env=settings.environment,
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(settings.environment["SMOKE_SCENARIO"], "analysis-version-replay")
            self.assertEqual(settings.environment["ANALYSIS_POLICY_REVISION"], "analysis-policy-v1")
            self.assertEqual(settings.environment["OPENAI_API_KEY"], "")
            _validate_model(settings, result.stdout)

    def test_deterministic_ui_scenario_uses_the_zero_model_fixture_stack(self) -> None:
        tokens = iter(("a" * 32, "b" * 32))
        with tempfile.TemporaryDirectory() as directory:
            settings = build_settings(
                ROOT,
                Path(directory),
                scenario="deterministic-finding-ui",
                token_hex=lambda _bytes: next(tokens),
                parent_environment={
                    "PATH": os.environ["PATH"],
                    "OPENAI_API_KEY": "secret",
                },
            )

            self.assertEqual(settings.environment["SMOKE_SCENARIO"], "deterministic-finding-ui")
            self.assertEqual(settings.environment["EVIDENCE_MODE"], "fixture")
            self.assertEqual(settings.environment["MODEL_MODE"], "fake")
            self.assertEqual(settings.environment["OPENAI_API_KEY"], "")

    def test_end_to_end_trace_scenario_uses_the_zero_model_fixture_stack(self) -> None:
        tokens = iter(("a" * 32, "b" * 32))
        with tempfile.TemporaryDirectory() as directory:
            settings = build_settings(
                ROOT,
                Path(directory),
                scenario="end-to-end-trace",
                token_hex=lambda _bytes: next(tokens),
                parent_environment={
                    "PATH": os.environ["PATH"],
                    "OPENAI_API_KEY": "secret",
                },
            )

            self.assertEqual(settings.environment["SMOKE_SCENARIO"], "end-to-end-trace")
            self.assertEqual(settings.environment["MODEL_MODE"], "fake")
            self.assertEqual(settings.environment["OPENAI_API_KEY"], "")

    def test_runtime_signals_scenario_uses_the_zero_model_fixture_stack(self) -> None:
        tokens = iter(("a" * 32, "b" * 32))
        with tempfile.TemporaryDirectory() as directory:
            settings = build_settings(
                ROOT,
                Path(directory),
                scenario="runtime-signals",
                token_hex=lambda _bytes: next(tokens),
                parent_environment={
                    "PATH": os.environ["PATH"],
                    "OPENAI_API_KEY": "secret",
                },
            )

            self.assertEqual(settings.environment["SMOKE_SCENARIO"], "runtime-signals")
            self.assertEqual(settings.environment["MODEL_MODE"], "fake")
            self.assertEqual(settings.environment["OPENAI_API_KEY"], "")

    def test_full_stack_drain_scenario_uses_the_zero_model_fixture_stack(self) -> None:
        tokens = iter(("a" * 32, "b" * 32))
        with tempfile.TemporaryDirectory() as directory:
            settings = build_settings(
                ROOT,
                Path(directory),
                scenario="full-stack-drain",
                token_hex=lambda _bytes: next(tokens),
                parent_environment={
                    "PATH": os.environ["PATH"],
                    "OPENAI_API_KEY": "secret",
                },
            )

            self.assertEqual(settings.environment["SMOKE_SCENARIO"], "full-stack-drain")
            self.assertEqual(settings.environment["MODEL_MODE"], "fake")
            self.assertEqual(settings.environment["OPENAI_API_KEY"], "")

    def test_worker_shutdown_scenario_starts_without_a_fake_provider_delay(
        self,
    ) -> None:
        tokens = iter(("a" * 32, "b" * 32))
        with tempfile.TemporaryDirectory() as directory:
            settings = build_settings(
                ROOT,
                Path(directory),
                scenario="worker-shutdown",
                token_hex=lambda _bytes: next(tokens),
                parent_environment={
                    "PATH": os.environ["PATH"],
                    "OPENAI_API_KEY": "secret",
                },
            )

            self.assertEqual(settings.environment["SMOKE_SCENARIO"], "worker-shutdown")
            self.assertEqual(settings.environment["FAKE_MODEL_DELAY_SECONDS"], "0")
            self.assertEqual(settings.environment["OPENAI_API_KEY"], "")

    def test_movement_stage_accepts_only_bounded_stable_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            progress = Path(directory) / "movement-stage"
            settings = mock.Mock()
            settings.environment = {"SMOKE_PROGRESS_FILE": str(progress)}

            progress.write_text("consumer_lag\n", encoding="utf-8")
            self.assertEqual(_movement_stage(settings), "consumer_lag")

            progress.write_text("unsafe stage: secret=value\n", encoding="utf-8")
            self.assertIsNone(_movement_stage(settings))

    def test_ephemeral_grafana_port_is_injected_into_the_api(self) -> None:
        settings = mock.Mock()
        settings.compose = ("docker", "compose")
        settings.environment = {}
        calls: list[tuple[str, ...]] = []

        def runner(command: tuple[str, ...], _timeout: float, _code: str) -> str:
            calls.append(command)
            return "127.0.0.1:43123\n" if command[-2:] == ("grafana", "3000") else ""

        _configure_grafana_public_url(settings, runner)

        self.assertEqual(
            settings.environment["GRAFANA_BASE_URL"],
            "http://127.0.0.1:43123",
        )
        self.assertEqual(
            calls,
            [
                ("docker", "compose", "port", "grafana", "3000"),
                (
                    "docker",
                    "compose",
                    "up",
                    "--detach",
                    "--no-deps",
                    "--force-recreate",
                    "--wait",
                    "--wait-timeout",
                    "300",
                    "api",
                ),
            ],
        )

    def test_cleanup_removes_verified_owned_resources_and_preserves_foreign(
        self,
    ) -> None:
        daemon = _FakeDockerDaemon()
        project = DockerProject(
            endpoint="unix:///var/run/docker.sock",
            project=PROJECT,
            owner=OWNER,
            run=daemon.run,
        )

        project.cleanup()

        self.assertEqual(
            daemon.removals,
            [
                ("container", CONTAINER_ID),
                ("network", NETWORK_ID),
                ("volume", VOLUME_NAME),
            ],
        )
        self.assertIn(FOREIGN_CONTAINER_ID, daemon.containers)

    def test_network_reservation_retries_and_verifies_project_ownership(self) -> None:
        daemon = _FakeDockerDaemon(
            owned_resources=False,
            network_create_failures=1,
        )
        project = DockerProject(
            endpoint="unix:///var/run/docker.sock",
            project=PROJECT,
            owner=OWNER,
            run=daemon.run,
        )

        project.reserve_networks()

        self.assertEqual(len(daemon.network_create_commands), 3)
        pipeline = daemon.networks[NETWORK_ID]
        observability = daemon.networks[OBS_NETWORK_ID]
        self.assertEqual(pipeline["name"], f"{PROJECT}_pipeline")
        self.assertEqual(
            observability["name"],
            f"{PROJECT}_docker-observability",
        )
        for network in (pipeline, observability):
            labels = network["labels"]
            self.assertEqual(labels["com.docker.compose.project"], PROJECT)
            self.assertEqual(
                labels["io.pypi-change-intelligence.smoke-owner"],
                OWNER,
            )
        self.assertIn("--internal", daemon.network_create_commands[-1])

    def test_cleanup_fails_closed_but_attempts_other_owned_resources(self) -> None:
        daemon = _FakeDockerDaemon(drift_container=True)
        project = DockerProject(
            endpoint="unix:///var/run/docker.sock",
            project=PROJECT,
            owner=OWNER,
            run=daemon.run,
        )

        with self.assertRaisesRegex(  # noqa: PT027 - platform suite uses unittest
            DockerProjectError,
            "docker_container_identity_changed",
        ):
            project.cleanup()

        self.assertEqual(
            daemon.removals,
            [
                ("network", NETWORK_ID),
                ("volume", VOLUME_NAME),
            ],
        )
        self.assertIn(CONTAINER_ID, daemon.containers)
        self.assertIn(FOREIGN_CONTAINER_ID, daemon.containers)

    def test_image_cleanup_attempts_later_tags_after_identity_failure(self) -> None:
        first_tag = f"{PROJECT}/api:local"
        second_tag = f"{PROJECT}/web:local"
        settings = mock.Mock()
        settings.image_tags = (first_tag, second_tag)
        with (
            mock.patch(
                "scripts.ci.isolated_smoke._image_tags",
                side_effect=(("foreign/image:latest",), (second_tag,), ()),
            ),
            mock.patch("scripts.ci.isolated_smoke._run_command") as run_command,
        ):
            with self.assertRaisesRegex(  # noqa: PT027 - platform suite uses unittest
                IsolatedSmokeError,
                "smoke_image_identity_invalid",
            ):
                _cleanup_images(settings)

        run_command.assert_called_once()
        self.assertEqual(run_command.call_args.args[1][-1], second_tag)

    def test_runner_cleans_on_success_failure_and_external_interruption(self) -> None:
        settings = mock.Mock()
        settings.root = ROOT
        settings.project = PROJECT
        settings.owner = OWNER
        settings.environment = {"PATH": "/usr/bin:/bin"}
        settings.image_tags = ()
        settings.compose = ("docker", "compose")
        for outcome in ("success", "failure", "interrupt"):
            with self.subTest(outcome=outcome):

                class ProjectStub:
                    def __init__(self) -> None:
                        self.cleanup_calls = 0

                    def assert_absent(self) -> None:
                        return

                    def diagnostic_states(self) -> tuple[dict[str, str], ...]:
                        return ()

                    def reserve_networks(self) -> None:
                        return

                    def cleanup(self) -> None:
                        self.cleanup_calls += 1

                project = ProjectStub()
                canary = ProjectStub()
                command_results: list[object] = ["{}"]
                if outcome == "failure":
                    command_results.append(IsolatedSmokeError("smoke_start_failed"))
                elif outcome == "interrupt":
                    command_results.append(ProcessInterrupted(15))
                else:
                    command_results.extend(("", "", "", "127.0.0.1:43123\n", "", ""))

                with (
                    mock.patch(
                        "scripts.ci.isolated_smoke.DockerProject",
                        side_effect=(project, canary),
                    ),
                    mock.patch(
                        "scripts.ci.isolated_smoke._run_command",
                        side_effect=command_results,
                    ),
                    mock.patch("scripts.ci.isolated_smoke._validate_model"),
                    mock.patch("scripts.ci.isolated_smoke._assert_images_absent"),
                    mock.patch("scripts.ci.isolated_smoke._start_canary"),
                    mock.patch("scripts.ci.isolated_smoke._verify_foreign_marker_absent"),
                    mock.patch("scripts.ci.isolated_smoke._cleanup_images"),
                ):
                    if outcome == "success":
                        run(settings)
                    elif outcome == "failure":
                        with self.assertRaises(  # noqa: PT027 - platform suite uses unittest
                            IsolatedSmokeError
                        ):
                            run(settings)
                    else:
                        with self.assertRaises(  # noqa: PT027 - platform suite uses unittest
                            ProcessInterrupted
                        ):
                            run(settings)

                self.assertEqual(project.cleanup_calls, 1)
                self.assertEqual(canary.cleanup_calls, 1)

    def test_operation_deadline_stops_work_and_cleanup_gets_fresh_budget(self) -> None:
        settings = mock.Mock()
        settings.root = ROOT
        settings.project = PROJECT
        settings.owner = OWNER
        settings.environment = {"PATH": "/usr/bin:/bin"}
        settings.image_tags = ()
        settings.compose = ("docker", "compose")
        clock = [0.0]

        class ProjectStub:
            def __init__(self) -> None:
                self.cleanup_calls = 0

            def assert_absent(self) -> None:
                return

            def diagnostic_states(self) -> tuple[dict[str, str], ...]:
                return ()

            def reserve_networks(self) -> None:
                return

            def cleanup(self) -> None:
                self.cleanup_calls += 1

        project = ProjectStub()
        canary = ProjectStub()
        commands: list[tuple[str, ...]] = []

        def run_command(
            _settings: object,
            command: tuple[str, ...],
            *,
            timeout_seconds: float,
            failure_code: str,
        ) -> str:
            del timeout_seconds, failure_code
            commands.append(command)
            if command[-1] == "build":
                clock[0] = 2_701.0
                return ""
            return "{}"

        with (
            mock.patch(
                "scripts.ci.isolated_smoke.DockerProject",
                side_effect=(project, canary),
            ),
            mock.patch(
                "scripts.ci.isolated_smoke._run_command",
                side_effect=run_command,
            ),
            mock.patch("scripts.ci.isolated_smoke._validate_model"),
            mock.patch("scripts.ci.isolated_smoke._assert_images_absent"),
            mock.patch("scripts.ci.isolated_smoke._start_canary"),
            mock.patch("scripts.ci.isolated_smoke._cleanup_images"),
        ):
            with self.assertRaisesRegex(  # noqa: PT027 - platform suite uses unittest
                IsolatedSmokeError,
                "smoke_operation_deadline_exceeded",
            ):
                run(settings, monotonic=lambda: clock[0])

        self.assertEqual(project.cleanup_calls, 1)
        self.assertEqual(canary.cleanup_calls, 1)
        self.assertFalse(any("create" in command for command in commands))


@unittest.skipUnless(
    os.environ.get("RUN_ISOLATED_SMOKE_SIGNAL_INTEGRATION") == "1",
    "set RUN_ISOLATED_SMOKE_SIGNAL_INTEGRATION=1 for real Docker signal tests",
)
class IsolatedSmokeSignalIntegrationTests(unittest.TestCase):
    def test_sigint_and_sigterm_clean_owned_resources_only(self) -> None:
        retained_before = self._retained_health()
        self._assert_no_isolated_resources()

        for signal_number, expected_exit in (
            (signal.SIGINT, 130),
            (signal.SIGTERM, 143),
        ):
            with self.subTest(signal=signal_number):
                process = subprocess.Popen(
                    (
                        "python3",
                        "-m",
                        "scripts.ci.isolated_smoke",
                        "--root",
                        str(ROOT),
                    ),
                    cwd=ROOT,
                    env={"PATH": os.environ["PATH"]},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                self._wait_for_isolated_container(process)
                process.send_signal(signal_number)
                stdout, stderr = process.communicate(timeout=240)

                self.assertEqual(process.returncode, expected_exit)
                self.assertNotIn("foreign-marker-", stdout)
                self.assertNotIn("foreign-marker-", stderr)
                self.assertIn("smoke_interrupted", stderr)
                self._assert_no_isolated_resources()
                self.assertEqual(self._retained_health(), retained_before)

    def _wait_for_isolated_container(self, process: subprocess.Popen[str]) -> None:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            projects = self._docker_lines(
                "container",
                "ls",
                "--all",
                "--format",
                '{{.Label "com.docker.compose.project"}}',
            )
            if any(
                project.startswith(("pypi-fixture-smoke-", "pypi-foreign-canary-"))
                for project in projects
            ):
                return
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(
                    f"isolated smoke exited before signal point: "
                    f"{process.returncode}; stdout={stdout!r}; stderr={stderr!r}"
                )
            time.sleep(0.2)
        process.kill()
        process.communicate(timeout=30)
        self.fail("timed out waiting for isolated smoke resources")

    def _assert_no_isolated_resources(self) -> None:
        for kind, action in (
            ("container", "ls"),
            ("network", "ls"),
            ("volume", "ls"),
        ):
            arguments = [kind, action]
            if kind == "container":
                arguments.append("--all")
            arguments.extend(
                (
                    "--format",
                    '{{.Label "com.docker.compose.project"}}',
                )
            )
            projects = self._docker_lines(*arguments)
            self.assertFalse(
                any(
                    project.startswith(("pypi-fixture-smoke-", "pypi-foreign-canary-"))
                    for project in projects
                ),
                f"isolated {kind} remained after cleanup",
            )
        image_tags = self._docker_lines(
            "image",
            "ls",
            "--format",
            "{{.Repository}}:{{.Tag}}",
        )
        self.assertFalse(
            any(tag.startswith("pypi-fixture-smoke-") for tag in image_tags),
            "isolated images remained after cleanup",
        )

    def _retained_health(self) -> dict[str, str]:
        container_ids = self._docker_lines(
            "container",
            "ls",
            "--no-trunc",
            "--filter",
            "label=com.docker.compose.project=pypi-change-intelligence",
            "--format",
            "{{.ID}}",
        )
        health: dict[str, str] = {}
        for container_id in container_ids:
            states = self._docker_lines(
                "container",
                "inspect",
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                container_id,
            )
            self.assertEqual(len(states), 1)
            health[container_id] = states[0]
        return health

    @staticmethod
    def _docker_lines(*arguments: str) -> tuple[str, ...]:
        result = subprocess.run(
            (
                "docker",
                "--host",
                "unix:///var/run/docker.sock",
                *arguments,
            ),
            cwd=ROOT,
            env={"PATH": os.environ["PATH"]},
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return tuple(line for line in result.stdout.splitlines() if line)


if __name__ == "__main__":
    unittest.main()
