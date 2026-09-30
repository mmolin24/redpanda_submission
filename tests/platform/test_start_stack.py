from __future__ import annotations

import unittest
from collections.abc import Mapping
from unittest import mock

from scripts.grafana import published_grafana_url
from scripts.start_stack import start_stack


class StartStackTests(unittest.TestCase):
    def test_discovered_port_overrides_stale_url_without_mutating_parent(self) -> None:
        compose = ("docker", "compose", "-p", "demo", "-f", "custom.yml")
        environment = {"GRAFANA_BASE_URL": "http://localhost:55000", "MODEL_MODE": "fake"}
        calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

        def runner(command: tuple[str, ...], env: Mapping[str, str]) -> str:
            calls.append((command, dict(env)))
            return "127.0.0.1:55003\n" if command[-3:] == ("port", "grafana", "3000") else ""

        self.assertEqual(start_stack(compose, environment, runner=runner), "http://127.0.0.1:55003")
        self.assertEqual(
            [command for command, _ in calls],
            [
                (*compose, "up", "--build", "--force-recreate", "--detach", "--wait"),
                (*compose, "port", "grafana", "3000"),
                (*compose, "up", "--detach", "--no-deps", "--force-recreate", "--wait", "api"),
            ],
        )
        self.assertEqual(calls[-1][1]["GRAFANA_BASE_URL"], "http://127.0.0.1:55003")
        self.assertEqual(calls[-1][1]["MODEL_MODE"], "fake")
        self.assertEqual(environment["GRAFANA_BASE_URL"], "http://localhost:55000")

    def test_sync_only_uses_existing_mapping_and_recreates_only_api(self) -> None:
        commands: list[tuple[str, ...]] = []

        def runner(command: tuple[str, ...], _env: Mapping[str, str]) -> str:
            commands.append(command)
            return "127.0.0.1:43123" if command[-3:] == ("port", "grafana", "3000") else ""

        url = start_stack(("docker", "compose"), {}, sync_only=True, runner=runner)
        self.assertEqual(url, "http://127.0.0.1:43123")
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[-1][-1], "api")
        self.assertIn("--no-deps", commands[-1])

    def test_invalid_mapping_does_not_recreate_api(self) -> None:
        for published in (
            "",
            "127.0.0.1:0",
            "127.0.0.1:65536",
            "0.0.0.0:3001",
            "garbage",
            "127.0.0.1:3001\n[::1]:3001",
        ):
            with self.subTest(published=published):
                runner = mock.Mock(return_value=published)
                with self.assertRaises(ValueError):  # noqa: PT027 - platform suite uses unittest
                    start_stack(("docker", "compose"), {}, sync_only=True, runner=runner)
                self.assertEqual(runner.call_count, 1)

    def test_loopback_bindings(self) -> None:
        for binding in ("127.0.0.1:3001", "localhost:3001", "[::1]:3001"):
            with self.subTest(binding=binding):
                self.assertEqual(published_grafana_url(binding), "http://127.0.0.1:3001")


if __name__ == "__main__":
    unittest.main()
