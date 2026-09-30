from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests.platform.compose_support import load_compose_model, load_yaml_document

ROOT = Path(__file__).resolve().parents[2]
DEMO_COMPOSE = ROOT / "docker-compose.override.yml"


class ComposeDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.model = load_compose_model(ROOT, additional_files=(DEMO_COMPOSE,))

    def service(self, name: str) -> dict[str, Any]:
        service = self.model["services"][name]
        self.assertIsInstance(service, dict)
        return service

    def assert_dependency(self, service: str, dependency: str, condition: str) -> None:
        self.assertEqual(
            self.service(service)["depends_on"][dependency]["condition"],
            condition,
        )

    def test_fixture_source_is_finite_and_precedes_fixture_reasoning(self) -> None:
        source = load_yaml_document(ROOT / "config/connect/source-demo-fixture.yaml")
        generate = source["input"]["generate"]
        self.assertEqual(generate["count"], 2)
        self.assertEqual(generate["mapping"], 'from "/config/fixture-once.blobl"')

        fixture_source = self.service("connect-fixture")
        self.assertEqual(fixture_source["restart"], "no")
        self.assertIn("/config/source-demo-fixture.yaml", fixture_source["command"])
        self.assert_dependency(
            "reasoning-fixture",
            "connect-fixture",
            "service_completed_successfully",
        )

    def test_fixture_worker_is_bounded_without_enabling_paid_calls(self) -> None:
        worker = self.service("reasoning-fixture")
        environment = worker["environment"]
        self.assertEqual(worker["restart"], "no")
        self.assertEqual(environment["EVIDENCE_MODE"], "fixture")
        self.assertEqual(environment["MODEL_MODE"], "fake")
        self.assertEqual(environment["OPENAI_API_KEY"], "")
        self.assertEqual(environment["EXIT_AFTER_IDLE_POLLS"], "2")

    def test_live_services_are_blocked_by_persistence_and_zero_lag_gates(self) -> None:
        self.assert_dependency(
            "demo-persistence-gate",
            "reasoning-fixture",
            "service_completed_successfully",
        )
        self.assert_dependency(
            "drain-gate",
            "demo-persistence-gate",
            "service_completed_successfully",
        )
        self.assert_dependency(
            "reasoning-worker",
            "drain-gate",
            "service_completed_successfully",
        )
        self.assert_dependency(
            "connect-source",
            "drain-gate",
            "service_completed_successfully",
        )
        self.assert_dependency(
            "connect-source",
            "reasoning-worker",
            "service_healthy",
        )
        self.assertEqual(
            self.service("reasoning-worker")["environment"]["EVIDENCE_MODE"],
            "pypi",
        )
        self.assertEqual(
            self.service("reasoning-worker")["environment"]["MODEL_MODE"],
            "auto",
        )
        self.assertIn("/config/source-live.yaml", self.service("connect-source")["command"])

    def test_demo_gates_are_one_shot_and_have_no_docker_socket(self) -> None:
        for service_name in (
            "demo-model-check",
            "demo-run-start",
            "demo-persistence-gate",
            "drain-gate",
        ):
            service = self.service(service_name)
            self.assertEqual(service["restart"], "no")
            self.assertNotIn("/var/run/docker.sock", str(service.get("volumes", "")))

    def test_demo_shell_gates_are_syntactically_valid(self) -> None:
        for relative_path in (
            "infra/demo/check-model.sh",
            "infra/demo/mark-start.sh",
            "infra/demo/check-persistence.sh",
            "infra/demo/check-lag.sh",
        ):
            subprocess.run(
                ("sh", "-n", str(ROOT / relative_path)),
                cwd=ROOT,
                check=True,
            )

    def test_model_gate_fails_closed_for_invalid_paid_configuration(self) -> None:
        script = ROOT / "infra/demo/check-model.sh"
        automatic = subprocess.run(
            ("sh", str(script)),
            env={"MODEL_MODE": "auto"},
            check=False,
        )
        fake = subprocess.run(("sh", str(script)), env={"MODEL_MODE": "fake"}, check=False)
        missing_key = subprocess.run(
            ("sh", str(script)),
            env={"MODEL_MODE": "openai"},
            check=False,
            capture_output=True,
            text=True,
        )
        unsupported = subprocess.run(
            ("sh", str(script)),
            env={"MODEL_MODE": "unsupported"},
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(automatic.returncode, 0)
        self.assertEqual(fake.returncode, 0)
        self.assertNotEqual(missing_key.returncode, 0)
        self.assertIn("OPENAI_API_KEY", missing_key.stderr)
        self.assertNotEqual(unsupported.returncode, 0)

    def test_lag_gate_requires_two_complete_zero_lag_samples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rpk = Path(directory) / "rpk"
            rpk.write_text(
                "#!/bin/sh\n"
                "cat <<EOF\n"
                "- group_name: fixture\n"
                "  total_lag: ${FAKE_LAG:-0}\n"
                "  partitions:\n"
                "    - partition: 0\n"
                "EOF\n",
                encoding="utf-8",
            )
            rpk.chmod(0o755)
            environment = {
                "PATH": f"{directory}:{os.environ['PATH']}",
                "DEMO_GATE_MAX_POLLS": "2",
                "DEMO_GATE_POLL_INTERVAL_SECONDS": "0",
            }
            passed = subprocess.run(
                ("sh", str(ROOT / "infra/demo/check-lag.sh")),
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            environment["FAKE_LAG"] = "1"
            blocked = subprocess.run(
                ("sh", str(ROOT / "infra/demo/check-lag.sh")),
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(passed.returncode, 0)
        self.assertEqual(passed.stdout.count("Zero-lag sample"), 2)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("two consecutive zero-lag", blocked.stderr)

    def test_readme_primary_demo_command_uses_grafana_discovery_launcher(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        command = "make up"
        self.assertIn(command, readme)
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertNotIn("demo:", makefile)
        self.assertIn("python3 -m scripts.start_stack", makefile)
        self.assertTrue(DEMO_COMPOSE.is_file())

    def test_readme_drain_command_stops_ingress_before_the_one_shot_gate(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        source_stop = "docker compose stop --timeout 60 connect-source"
        drain_gate = "docker compose run --rm --no-deps drain-gate"
        self.assertIn(f"{source_stop} && \\\n  {drain_gate}", readme)
        self.assertLess(readme.index(source_stop), readme.index(drain_gate))
        self.assertNotIn("/var/run/docker.sock", str(self.service("drain-gate")))

    def test_makefile_exposes_only_the_small_local_interface(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        targets = {
            line.removesuffix(":")
            for line in makefile.splitlines()
            if line and not line.startswith(("\t", ".")) and line.endswith(":")
        }
        self.assertEqual(
            targets,
            {"help", "format", "check", "smoke", "up", "up-fixture", "stop-safe"},
        )
        self.assertIn("sh scripts/check.sh", makefile)


if __name__ == "__main__":
    unittest.main()
