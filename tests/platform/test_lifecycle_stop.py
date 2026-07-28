from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from infra.lifecycle import CONSUMER_GROUPS
from tests.platform.compose_support import load_yaml_document

ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE = ROOT / "infra" / "lifecycle.py"
PROJECT = "pypi-lifecycle-test"
GROUPS = (
    ("pypi-reasoning-v1", "reasoning"),
    ("pypi-postgres-sink-v1", "sink"),
    ("pypi-connect-trace-bridge-v1", "trace"),
)
DOCKER_ENDPOINT_FORMAT = '{{(index .Endpoints "docker").Host}}'


class LifecycleStopTests(unittest.TestCase):
    def test_drain_stops_ingress_without_stopping_the_remaining_services(
        self,
    ) -> None:
        with _LifecycleEnvironment() as environment:
            result = self._run(environment, command="drain")
            calls = environment.docker_calls()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            calls,
            [
                f"context inspect --format {DOCKER_ENDPOINT_FORMAT}",
                *self._compose_calls(
                    "ps -a -q",
                    "stop --timeout 60 connect-source",
                    *self._group_sweeps(2),
                ),
            ],
        )
        self.assertIn("services remain running", result.stdout)
        self.assertFalse(any(" down " in f" {call} " for call in calls))
        self._assert_no_volume_deletion(calls)

    def test_stop_drains_two_complete_samples_before_non_destructive_down(
        self,
    ) -> None:
        with _LifecycleEnvironment() as environment:
            result = self._run(environment)
            calls = environment.docker_calls()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            calls,
            [
                f"context inspect --format {DOCKER_ENDPOINT_FORMAT}",
                *self._compose_calls(
                    "ps -a -q",
                    "stop --timeout 60 connect-source",
                    *self._group_sweeps(2),
                    "down --timeout 60 --remove-orphans",
                ),
            ],
        )
        self._assert_no_volume_deletion(calls)

    def test_nonzero_lag_resets_the_consecutive_sample_count(self) -> None:
        with _LifecycleEnvironment(
            extra_environment={"FAKE_REASONING_LAGS": "0,1,0,0"},
        ) as environment:
            result = self._run(environment, max_polls=4)
            calls = environment.docker_calls()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            calls,
            [
                f"context inspect --format {DOCKER_ENDPOINT_FORMAT}",
                *self._compose_calls(
                    "ps -a -q",
                    "stop --timeout 60 connect-source",
                    *self._group_sweeps(4),
                    "down --timeout 60 --remove-orphans",
                ),
            ],
        )

    def test_untouched_empty_partition_counts_as_drained(self) -> None:
        with _LifecycleEnvironment(
            extra_environment={
                "FAKE_REASONING_LAGS": "untouched_empty,untouched_empty",
                "FAKE_SINK_LAGS": "untouched_empty,untouched_empty",
            },
        ) as environment:
            result = self._run(environment)
            calls = environment.docker_calls()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            calls,
            [
                f"context inspect --format {DOCKER_ENDPOINT_FORMAT}",
                *self._compose_calls(
                    "ps -a -q",
                    "stop --timeout 60 connect-source",
                    *self._group_sweeps(2),
                    "down --timeout 60 --remove-orphans",
                ),
            ],
        )

    def test_failed_or_malformed_lag_never_forces_shutdown(self) -> None:
        with _LifecycleEnvironment(
            extra_environment={
                "FAKE_REASONING_LAGS": "malformed,error,empty,1",
                "SECRET_SENTINEL": "retained-content-must-not-print",
            },
        ) as environment:
            result = self._run(environment, max_polls=4)
            calls = environment.docker_calls()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ingress remains stopped", result.stderr)
        self.assertNotIn(
            "retained-content-must-not-print",
            result.stdout + result.stderr,
        )
        self.assertFalse(any(" down " in f" {call} " for call in calls))
        self._assert_no_volume_deletion(calls)

    def test_incomplete_or_inconsistent_group_response_never_counts_as_drained(
        self,
    ) -> None:
        for payload_case in (
            "wrong_group",
            "reported_error",
            "missing_group",
            "missing_partition_identity",
            "duplicate_partition",
            "multiple_groups",
            "inconsistent_total",
            "boolean_lag",
            "missing_group_summary",
            "missing_offset_field",
            "current_offset_below_sentinel",
            "sentinel_with_data",
            "negative_log_start",
            "negative_log_end",
            "negative_lag",
        ):
            with self.subTest(payload_case=payload_case):
                with _LifecycleEnvironment(
                    extra_environment={
                        "FAKE_REASONING_LAGS": f"{payload_case},0",
                    },
                ) as environment:
                    result = self._run(environment, max_polls=2)
                    calls = environment.docker_calls()

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ingress remains stopped", result.stderr)
                self.assertFalse(any(" down " in f" {call} " for call in calls))
                self._assert_no_volume_deletion(calls)

    def test_source_stop_and_down_failures_are_static_and_fail_closed(self) -> None:
        cases = (
            (
                "source stop",
                {"FAKE_STOP_STATUS": "1"},
                False,
                "Lifecycle stop failed: Ingress stop did not complete; "
                "source state is unknown, so inspect project state before retrying.\n",
            ),
            (
                "shutdown",
                {"FAKE_DOWN_STATUS": "1"},
                True,
                "Lifecycle stop failed: Shutdown failed after drain; ingress remains "
                "stopped and volumes are intact.\n",
            ),
        )
        for name, overrides, should_probe, expected_stderr in cases:
            with self.subTest(case=name):
                with _LifecycleEnvironment(extra_environment=overrides) as environment:
                    result = self._run(environment)
                    calls = environment.docker_calls()

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, expected_stderr)
                self.assertEqual(
                    any("group describe" in call for call in calls),
                    should_probe,
                )
                self._assert_no_volume_deletion(calls)

    def test_already_absent_project_is_a_truthful_noop(self) -> None:
        with _LifecycleEnvironment(
            extra_environment={"FAKE_PROJECT_CONTAINERS": ""},
        ) as environment:
            result = self._run(environment)
            calls = environment.docker_calls()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("already stopped", result.stdout)
        self.assertEqual(
            calls,
            [
                f"context inspect --format {DOCKER_ENDPOINT_FORMAT}",
                *self._compose_calls("ps -a -q"),
            ],
        )

    def test_remote_docker_endpoint_is_rejected_before_project_access(self) -> None:
        marker = "remote-endpoint-must-not-print"
        cases = (
            (
                "context",
                {"FAKE_DOCKER_ENDPOINT": f"ssh://{marker}"},
                [f"context inspect --format {DOCKER_ENDPOINT_FORMAT}"],
            ),
            (
                "DOCKER_HOST",
                {"DOCKER_HOST": f"tcp://{marker}:2376"},
                [],
            ),
        )
        for name, overrides, expected_calls in cases:
            with self.subTest(case=name):
                with _LifecycleEnvironment(
                    extra_environment=overrides,
                ) as environment:
                    result = self._run(environment)
                    calls = environment.docker_calls()

                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(marker, result.stdout + result.stderr)
                self.assertEqual(calls, expected_calls)

    def test_local_docker_host_skips_context_lookup(self) -> None:
        with _LifecycleEnvironment(
            extra_environment={"DOCKER_HOST": "unix:///tmp/test-docker.sock"},
        ) as environment:
            result = self._run(environment)
            calls = environment.docker_calls()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            calls,
            self._compose_calls(
                "ps -a -q",
                "stop --timeout 60 connect-source",
                *self._group_sweeps(2),
                "down --timeout 60 --remove-orphans",
            ),
        )

    def test_validated_endpoint_is_pinned_for_every_project_command(self) -> None:
        endpoint = "unix:///tmp/pinned-test-docker.sock"
        with _LifecycleEnvironment(
            extra_environment={
                "DOCKER_CONTEXT": "mutable-context",
                "FAKE_DOCKER_ENDPOINT": endpoint,
            },
        ) as environment:
            result = self._run(environment)
            docker_environments = environment.docker_environments()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            docker_environments[0],
            ("unset", "mutable-context"),
        )
        self.assertTrue(docker_environments[1:])
        self.assertEqual(
            docker_environments[1:],
            [(endpoint, "unset")] * (len(docker_environments) - 1),
        )

    def test_probe_timeout_is_bounded_and_never_reaches_down(self) -> None:
        started = time.monotonic()
        with _LifecycleEnvironment(
            extra_environment={"FAKE_GROUP_DELAY_SECONDS": "1"},
        ) as environment:
            result = self._run(
                environment,
                max_polls=5,
                probe_timeout=0.05,
                drain_timeout=0.2,
            )
            calls = environment.docker_calls()
        elapsed = time.monotonic() - started

        self.assertNotEqual(result.returncode, 0)
        self.assertLess(elapsed, 1.0)
        self.assertFalse(any(" down " in f" {call} " for call in calls))
        self._assert_no_volume_deletion(calls)

    def test_probe_timeout_terminates_the_entire_command_process_group(self) -> None:
        with _LifecycleEnvironment(
            extra_environment={"DOCKER_HOST": "unix:///tmp/test-docker.sock"},
        ) as environment:
            marker = environment.repository / "escaped-descendant"
            started = environment.repository / "descendant-started"
            environment.process_environment["FAKE_GROUP_DESCENDANT_MARKER"] = str(
                marker,
            )
            environment.process_environment["FAKE_GROUP_DESCENDANT_STARTED"] = str(
                started,
            )
            result = self._run(
                environment,
                max_polls=1,
                probe_timeout=1,
                drain_timeout=1.2,
            )
            calls = environment.docker_calls()
            time.sleep(1.75)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(started.exists())
            self.assertTrue(any("group describe" in call for call in calls))
            self.assertFalse(
                marker.exists(),
                "a timed-out Docker command left a descendant process running",
            )

    def test_invalid_project_name_fails_before_docker(self) -> None:
        with _LifecycleEnvironment() as environment:
            result = self._run(environment, project="../normal-project")
            calls = environment.docker_calls()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_make_entrypoint_passes_project_name_as_non_executable_data(self) -> None:
        with _LifecycleEnvironment() as environment:
            marker = environment.repository / "make-injection"
            project = f'safe"; printf INJECTED; touch {marker}; printf "'
            result = subprocess.run(
                [
                    "make",
                    "--no-print-directory",
                    "stop-safe",
                    f"LIFECYCLE_PROJECT={project}",
                ],
                cwd=ROOT,
                env=environment.process_environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            calls = environment.docker_calls()
            self.assertFalse(marker.exists())

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("INJECTED", result.stdout + result.stderr)
        self.assertEqual(calls, [])

    def _run(
        self,
        environment: _LifecycleEnvironment,
        *,
        command: str = "stop",
        project: str = PROJECT,
        max_polls: int = 3,
        probe_timeout: float = 1,
        drain_timeout: float = 5,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(LIFECYCLE),
                "--root",
                str(environment.repository),
                "--project-name",
                project,
                "--max-polls",
                str(max_polls),
                "--poll-interval-seconds",
                "0",
                "--probe-timeout-seconds",
                str(probe_timeout),
                "--drain-timeout-seconds",
                str(drain_timeout),
                command,
            ],
            cwd=ROOT,
            env=environment.process_environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def _compose_calls(self, *suffixes: str) -> list[str]:
        prefix = (
            f"compose --project-name {PROJECT} --env-file /dev/null "
            f"-f {{repository}}/docker-compose.yml --project-directory {{repository}} "
        )
        return [prefix + suffix for suffix in suffixes]

    @staticmethod
    def _group_sweeps(count: int) -> list[str]:
        calls: list[str] = []
        for _ in range(count):
            for group, _key in GROUPS:
                calls.append(
                    "exec -T redpanda rpk group describe "
                    f"{group} -X brokers=redpanda:9092 --format json",
                )
        return calls

    def _assert_no_volume_deletion(self, calls: list[str]) -> None:
        rendered = "\n".join(calls)
        for forbidden in (
            "--volumes",
            "volume rm",
            "volume prune",
            "system prune",
        ):
            self.assertNotIn(forbidden, rendered)


class LifecycleConfigurationTests(unittest.TestCase):
    def test_drain_groups_match_every_data_movement_consumer(self) -> None:
        compose = load_yaml_document(ROOT / "docker-compose.yml")
        sink = load_yaml_document(ROOT / "config" / "connect" / "sink.yaml")
        trace_bridge = load_yaml_document(
            ROOT / "config" / "connect" / "trace-bridge.yaml",
        )

        self.assertEqual(
            CONSUMER_GROUPS,
            (
                compose["services"]["reasoning-worker"]["environment"]["CONSUMER_GROUP"],
                sink["input"]["redpanda"]["consumer_group"],
                trace_bridge["input"]["redpanda"]["consumer_group"],
            ),
        )


class _LifecycleEnvironment:
    def __init__(
        self,
        *,
        extra_environment: dict[str, str] | None = None,
    ) -> None:
        self._extra_environment = extra_environment or {}
        self._temporary = tempfile.TemporaryDirectory()

    def __enter__(self) -> _LifecycleEnvironment:
        temporary = Path(self._temporary.name)
        self.repository = (temporary / "repository").resolve()
        self.repository.mkdir()
        (self.repository / "docker-compose.yml").write_text(
            "name: pypi-change-intelligence\nservices: {}\n",
            encoding="utf-8",
        )
        fake_bin = temporary / "bin"
        fake_bin.mkdir()
        self._docker_log = temporary / "docker.log"
        self._docker_environment_log = temporary / "docker-environment.log"
        self._state_directory = temporary / "state"
        self._state_directory.mkdir()
        docker = fake_bin / "docker"
        docker.write_text(
            """#!/bin/sh
printf '%s\\n' "$*" >>"${FAKE_DOCKER_LOG}"
printf '%s\\t%s\\n' "${DOCKER_HOST-unset}" "${DOCKER_CONTEXT-unset}" \
  >>"${FAKE_DOCKER_ENV_LOG}"
case "$*" in
  "context inspect --format "*)
    printf '%s\\n' "${FAKE_DOCKER_ENDPOINT:-unix:///tmp/test-docker.sock}"
    exit "${FAKE_CONTEXT_STATUS:-0}"
    ;;
  *" ps -a -q")
    printf '%s\\n' "${FAKE_PROJECT_CONTAINERS-container-id}"
    exit "${FAKE_PS_STATUS:-0}"
    ;;
  *" stop --timeout 60 connect-source")
    [ "${FAKE_STOP_STATUS:-0}" -eq 0 ] || printf 'docker-test-error\\n' >&2
    exit "${FAKE_STOP_STATUS:-0}"
    ;;
  *" group describe "*)
    [ -z "${FAKE_GROUP_DELAY_SECONDS:-}" ] ||
      /bin/sleep "${FAKE_GROUP_DELAY_SECONDS}"
    if [ -n "${FAKE_GROUP_DESCENDANT_MARKER:-}" ]; then
      exec python3 -c '
import subprocess
import sys
import time
from pathlib import Path

descendant = subprocess.Popen(
    [
        "/bin/sh",
        "-c",
        "sleep 1.5; printf escaped >\\"$1\\"",
        "descendant",
        sys.argv[1],
    ],
)
Path(sys.argv[2]).write_text(f"{descendant.pid}\\n", encoding="utf-8")
time.sleep(10)
' "${FAKE_GROUP_DESCENDANT_MARKER}" "${FAKE_GROUP_DESCENDANT_STARTED}"
    fi
    case "$*" in
      *"pypi-reasoning-v1"*)
        key=reasoning
        group_name=pypi-reasoning-v1
        ;;
      *"pypi-postgres-sink-v1"*)
        key=sink
        group_name=pypi-postgres-sink-v1
        ;;
      *"pypi-connect-trace-bridge-v1"*)
        key=trace
        group_name=pypi-connect-trace-bridge-v1
        ;;
      *) exit 64 ;;
    esac
    counter="${FAKE_STATE_DIRECTORY}/${key}"
    index=0
    [ ! -f "${counter}" ] || IFS= read -r index <"${counter}"
    index=$((index + 1))
    printf '%s\\n' "${index}" >"${counter}"
    case "${key}" in
      reasoning) values="${FAKE_REASONING_LAGS:-0,0}" ;;
      sink) values="${FAKE_SINK_LAGS:-0,0}" ;;
      trace) values="${FAKE_TRACE_LAGS:-0,0}" ;;
    esac
    selected=
    old_ifs="${IFS}"
    IFS=,
    set -- ${values}
    IFS="${old_ifs}"
    position=1
    for value in "$@"; do
      selected="${value}"
      [ "${position}" -lt "${index}" ] || break
      position=$((position + 1))
    done
    case "${selected}" in
      error)
        printf 'docker-test-error\\n' >&2
        exit 1
        ;;
      malformed)
        printf '{\\n'
        ;;
      *)
        python3 -c '
import json
import sys

group_name, selected = sys.argv[1:]
partition = {
    "partition": 0,
    "current_offset": 0,
    "log_start_offset": 0,
    "log_end_offset": 0,
    "lag": 0,
    "topic": "test.topic",
    "member_id": "member",
    "client_id": "client",
    "host": "local",
}
member = {
    "member_id": "member",
    "client_id": "client",
    "host": "local",
    "topic_partitions": [],
}
group = {
    "group_name": group_name,
    "coordinator_partition": "__consumer_offsets/0",
    "state": "Stable",
    "balancer": "range",
    "members": 1,
    "coordinator_node": 0,
    "total_lag": 0,
    "partitions": [partition],
    "members_details": [member],
}
groups = [group]
if selected == "empty":
    group["partitions"] = []
elif selected == "wrong_group":
    group["group_name"] = "other"
elif selected == "reported_error":
    group["error"] = "failed"
elif selected == "missing_group":
    del group["group_name"]
elif selected == "missing_partition_identity":
    del partition["topic"]
elif selected == "duplicate_partition":
    group["partitions"] = [partition, dict(partition)]
elif selected == "multiple_groups":
    extra = dict(group)
    extra["group_name"] = "extra"
    groups.append(extra)
elif selected == "inconsistent_total":
    group["total_lag"] = 1
elif selected == "boolean_lag":
    partition["lag"] = False
elif selected == "missing_group_summary":
    del group["coordinator_partition"]
elif selected == "missing_offset_field":
    del partition["current_offset"]
elif selected == "untouched_empty":
    partition["current_offset"] = -1
elif selected == "current_offset_below_sentinel":
    partition["current_offset"] = -2
elif selected == "sentinel_with_data":
    partition["current_offset"] = -1
    partition["log_end_offset"] = 1
elif selected == "negative_log_start":
    partition["log_start_offset"] = -1
elif selected == "negative_log_end":
    partition["log_end_offset"] = -1
elif selected == "negative_lag":
    partition["lag"] = -1
else:
    lag = int(selected)
    partition["lag"] = lag
    partition["log_end_offset"] = lag
    group["total_lag"] = lag
print(json.dumps(groups, separators=(",", ":")))
' "${group_name}" "${selected}"
        ;;
    esac
    exit 0
    ;;
  *" down --timeout 60 --remove-orphans")
    [ "${FAKE_DOWN_STATUS:-0}" -eq 0 ] || printf 'docker-test-error\\n' >&2
    exit "${FAKE_DOWN_STATUS:-0}"
    ;;
esac
exit 64
""",
            encoding="utf-8",
        )
        docker.chmod(0o755)
        self.process_environment = {
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_DOCKER_LOG": str(self._docker_log),
            "FAKE_DOCKER_ENV_LOG": str(self._docker_environment_log),
            "FAKE_STATE_DIRECTORY": str(self._state_directory),
            **self._extra_environment,
        }
        return self

    def docker_calls(self) -> list[str]:
        if not self._docker_log.exists():
            return []
        repository = str(self.repository)
        return [
            line.replace(repository, "{repository}")
            for line in self._docker_log.read_text(encoding="utf-8").splitlines()
        ]

    def docker_environments(self) -> list[tuple[str, str]]:
        if not self._docker_environment_log.exists():
            return []
        records = []
        for line in self._docker_environment_log.read_text(
            encoding="utf-8",
        ).splitlines():
            docker_host, docker_context = line.split("\t", maxsplit=1)
            records.append((docker_host, docker_context))
        return records

    def __exit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> None:
        self._temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
