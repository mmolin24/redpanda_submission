from __future__ import annotations

import json
import os
import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from unittest import mock

from tests.platform.compose_support import (
    compose_subprocess_environment,
    load_compose_model,
    load_yaml_document,
)

ROOT = Path(__file__).resolve().parents[2]
IMMUTABLE_IMAGE_PATTERN = re.compile(r"^[^@\s]+:[^@\s]+@sha256:[0-9a-f]{64}$")
REVIEWED_LOCAL_BUILD_IMAGES = {
    "alloy": "pypi-change-intelligence/alloy:1.16.1",
    "api": "pypi-change-intelligence/api:local",
    "loki": "pypi-change-intelligence/loki:3.7.2",
    "reasoning-worker": "pypi-change-intelligence/reasoning-worker:local",
    "tempo": "pypi-change-intelligence/tempo:2.10.5",
    "web": "pypi-change-intelligence/web:local",
}


class PlatformConfigTests(unittest.TestCase):
    def _compose_image_contract_violations(
        self,
        model: dict[str, Any],
    ) -> list[str]:
        violations = []
        for service_name, service in model["services"].items():
            if not service.get("build"):
                continue
            actual = service.get("image")
            expected = REVIEWED_LOCAL_BUILD_IMAGES.get(service_name)
            if expected is None:
                violations.append(
                    f"Compose build service {service_name!r} does not have a "
                    f"reviewed local output; got {actual!r}"
                )
            elif actual != expected:
                violations.append(
                    f"Compose build service {service_name!r} must use the "
                    f"reviewed local output {expected!r}, got {actual!r}"
                )
        return violations

    def _load_compose_model(
        self,
        overrides: dict[str, str] | None = None,
        *,
        include_all_profiles: bool = False,
        additional_files: tuple[Path, ...] = (),
    ) -> dict[str, Any]:
        return load_compose_model(
            ROOT,
            overrides=overrides,
            include_all_profiles=include_all_profiles,
            additional_files=additional_files,
        )

    def _assert_immutable_image(self, image: str, location: str) -> None:
        self.assertRegex(
            image,
            IMMUTABLE_IMAGE_PATTERN,
            f"{location} must use a readable tag plus an immutable digest",
        )

    def _load_yaml(self, path: Path) -> dict[str, Any]:
        return load_yaml_document(path)

    def test_compose_configuration_is_valid_and_images_are_pinned(self) -> None:
        model = self._load_compose_model(include_all_profiles=True)
        required = {
            "redpanda",
            "redpanda-init",
            "postgres",
            "db-init",
            "connect-source",
            "reasoning-worker",
            "connect-sink",
            "connect-trace-bridge",
            "api",
            "web",
            "alloy",
            "tempo",
            "loki",
            "prometheus",
            "postgres-exporter",
            "grafana",
        }
        self.assertTrue(required.issubset(model["services"]))
        self.assertEqual(self._compose_image_contract_violations(model), [])
        for service_name, service in model["services"].items():
            image = service.get("image")
            build = service.get("build")
            if image and not build:
                self._assert_immutable_image(
                    image,
                    f"Compose service {service_name!r}",
                )
            if build:
                for argument, value in build.get("args", {}).items():
                    if argument == "BASE_IMAGE":
                        self._assert_immutable_image(
                            value,
                            f"Compose build argument {service_name}.{argument}",
                        )

    def test_compose_resolution_does_not_ingest_host_secrets(self) -> None:
        sentinel = "postgresql://user:credential-sentinel@outside.invalid/db"
        with mock.patch.dict(os.environ, {"DATABASE_URL": sentinel}):
            model = self._load_compose_model()
        self.assertNotIn("credential-sentinel", json.dumps(model))

    def test_external_build_output_cannot_use_the_local_image_exemption(
        self,
    ) -> None:
        model = self._load_compose_model()
        model["services"]["web"]["image"] = "external.example/web:latest"
        self.assertEqual(
            self._compose_image_contract_violations(model),
            [
                "Compose build service 'web' must use the reviewed local "
                "output 'pypi-change-intelligence/web:local', got "
                "'external.example/web:latest'"
            ],
        )

    def test_dockerfile_and_test_runner_images_are_immutable_and_consistent(
        self,
    ) -> None:
        references_by_tag: dict[str, set[str]] = {}

        def record(image: str, location: str) -> None:
            self._assert_immutable_image(image, location)
            tag, digest = image.rsplit("@", 1)
            references_by_tag.setdefault(tag, set()).add(digest)

        for service_name, service in self._load_compose_model(include_all_profiles=True)[
            "services"
        ].items():
            if image := service.get("image"):
                if not service.get("build"):
                    record(image, f"Compose service {service_name!r}")
            if build := service.get("build"):
                for argument, value in build.get("args", {}).items():
                    if argument == "BASE_IMAGE":
                        record(
                            value,
                            f"Compose build argument {service_name}.{argument}",
                        )

        for dockerfile in sorted(ROOT.rglob("Dockerfile")):
            for line_number, line in enumerate(
                dockerfile.read_text().splitlines(),
                start=1,
            ):
                if match := re.match(r"^ARG BASE_IMAGE=(\S+)$", line):
                    record(
                        match.group(1),
                        f"{dockerfile.relative_to(ROOT)}:{line_number}",
                    )
                if not line.startswith("FROM "):
                    continue
                tokens = line.split()
                image = next(token for token in tokens[1:] if not token.startswith("--"))
                if image == "scratch" or image.startswith("${"):
                    continue
                record(
                    image,
                    f"{dockerfile.relative_to(ROOT)}:{line_number}",
                )

        shell_assignment_pattern = re.compile(
            r'^([a-z_][a-z0-9_]*image)="([^"]+)"$',
            re.MULTILINE,
        )
        for script in sorted((ROOT / "infra").rglob("*.sh")):
            for variable, image in shell_assignment_pattern.findall(script.read_text()):
                record(
                    image,
                    f"{script.relative_to(ROOT)} variable {variable}",
                )

        self.assertGreater(len(references_by_tag), 0)
        self.assertEqual(
            {
                tag: sorted(digests)
                for tag, digests in references_by_tag.items()
                if len(digests) != 1
            },
            {},
            "the same tagged image must resolve to one checked-in digest",
        )

    def test_all_published_ports_are_loopback_only_and_overrides_survive(self) -> None:
        review_surface_contract = {
            ("redpanda-console", 8080): ("REDPANDA_CONSOLE_PORT", "8080", "18080"),
            ("api", 8000): ("API_PORT", "8000", "18000"),
            ("web", 8080): ("WEB_PORT", "3000", "13300"),
            ("grafana", 3000): ("GRAFANA_PORT", "3001", "13001"),
        }
        smoke_probe_contract = {
            **review_surface_contract,
            ("redpanda", 9644): ("REDPANDA_ADMIN_PORT", "", ""),
            ("connect-source", 4195): ("CONNECT_SOURCE_PORT", "", ""),
            ("reasoning-worker", 8090): ("REASONING_HEALTH_PORT", "", ""),
            ("reasoning-worker", 8001): ("REASONING_METRICS_PORT", "", ""),
            ("connect-sink", 4196): ("CONNECT_SINK_PORT", "", ""),
            ("connect-trace-bridge", 4197): ("CONNECT_TRACE_BRIDGE_PORT", "", ""),
            ("tempo", 3200): ("TEMPO_PORT", "", ""),
            ("loki", 3100): ("LOKI_PORT", "", ""),
            ("prometheus", 9090): ("PROMETHEUS_PORT", "", ""),
        }
        self.assertEqual(len(review_surface_contract), 4)
        self.assertEqual(len(smoke_probe_contract), 13)

        base_environment = compose_subprocess_environment()
        override_environment = {
            **base_environment,
            **{
                environment_name: override
                for environment_name, _default, override in review_surface_contract.values()
            },
        }
        default_model = self._load_compose_model(base_environment)
        override_model = self._load_compose_model(override_environment)
        smoke_model = self._load_compose_model(
            {
                **base_environment,
                **{
                    environment_name: ""
                    for environment_name, _default, _override in smoke_probe_contract.values()
                },
            },
            additional_files=(ROOT / "infra" / "smoke.compose.yml",),
        )

        for model, expected_count in (
            (default_model, 4),
            (override_model, 4),
            (smoke_model, 13),
        ):
            published = [
                (service_name, port)
                for service_name, service in model["services"].items()
                for port in service.get("ports", [])
            ]
            self.assertEqual(len(published), expected_count)
            self.assertEqual(
                {
                    service_name: port.get("host_ip", "<all-interfaces>")
                    for service_name, port in published
                    if port.get("host_ip") != "127.0.0.1"
                },
                {},
            )

        default_ports = {
            (service_name, port["target"]): str(port["published"])
            for service_name, service in default_model["services"].items()
            for port in service.get("ports", [])
        }
        self.assertEqual(
            default_ports,
            {
                service_port: default
                for service_port, (
                    _environment,
                    default,
                    _override,
                ) in review_surface_contract.items()
            },
        )

        override_ports = {
            (service_name, port["target"]): str(port["published"])
            for service_name, service in override_model["services"].items()
            for port in service.get("ports", [])
        }
        self.assertEqual(
            override_ports,
            {
                service_port: override
                for service_port, (
                    _environment,
                    _default,
                    override,
                ) in review_surface_contract.items()
            },
        )
        self.assertEqual(
            {
                str(port.get("published", ""))
                for service in smoke_model["services"].values()
                for port in service.get("ports", [])
            },
            {""},
        )

        default_redpanda_command = default_model["services"]["redpanda"]["command"]
        self.assertIn(
            "--advertise-kafka-addr=internal://redpanda:9092",
            default_redpanda_command,
        )
        self.assertFalse(any("pandaproxy" in item for item in default_redpanda_command))
        self.assertFalse(any("schema-registry" in item for item in default_redpanda_command))
        self.assertNotIn(
            "schemaRegistry",
            default_model["services"]["redpanda-console"]["environment"]["CONSOLE_CONFIG_FILE"],
        )
        self.assertEqual(
            override_model["services"]["api"]["environment"]["GRAFANA_BASE_URL"],
            "http://localhost:13001",
        )
        self.assertEqual(
            default_model["services"]["api"]["environment"]["GRAFANA_BASE_URL"],
            "http://localhost:3001",
        )
        external_grafana_model = self._load_compose_model(
            {
                "GRAFANA_PORT": "13001",
                "GRAFANA_BASE_URL": "https://observability.example/grafana",
            }
        )
        self.assertEqual(
            external_grafana_model["services"]["api"]["environment"]["GRAFANA_BASE_URL"],
            "https://observability.example/grafana",
        )

    def test_long_running_services_bound_logs_and_restart_state_stores(self) -> None:
        services = self._load_compose_model()["services"]
        long_running = {
            "redpanda",
            "redpanda-console",
            "postgres",
            "connect-source",
            "reasoning-worker",
            "connect-sink",
            "connect-trace-bridge",
            "api",
            "web",
            "docker-socket-proxy",
            "alloy",
            "tempo",
            "loki",
            "prometheus",
            "postgres-exporter",
            "grafana",
        }
        for service_name in long_running:
            with self.subTest(service=service_name):
                self.assertEqual(
                    services[service_name]["logging"],
                    {
                        "driver": "json-file",
                        "options": {"max-file": "3", "max-size": "20m"},
                    },
                )
        self.assertEqual(services["redpanda"]["restart"], "unless-stopped")
        self.assertEqual(services["postgres"]["restart"], "unless-stopped")
        self.assertEqual(services["redpanda-init"]["restart"], "no")
        self.assertEqual(services["db-init"]["restart"], "no")

    def test_ingestion_and_worker_share_the_read_only_monitored_package_config(
        self,
    ) -> None:
        services = self._load_compose_model(compose_subprocess_environment())["services"]
        for consumer in ("connect-source", "reasoning-worker"):
            self.assertEqual(
                services[consumer]["environment"]["MONITORED_PACKAGES_PATH"],
                "/project-config/monitored-packages.json",
            )
            mounts = [
                mount
                for mount in services[consumer]["volumes"]
                if mount["target"] == "/project-config"
            ]
            self.assertEqual(len(mounts), 1)
            self.assertTrue(mounts[0]["read_only"])

    def test_docker_socket_is_restricted_to_internal_read_only_proxy(self) -> None:
        model = self._load_compose_model()
        services = model["services"]
        socket_mounts = [
            (
                service_name,
                volume["target"],
                volume.get("read_only", False),
            )
            for service_name, service in services.items()
            for volume in service.get("volumes", [])
            if volume.get("source") == "/var/run/docker.sock"
        ]
        self.assertEqual(
            socket_mounts,
            [("docker-socket-proxy", "/var/run/docker.sock", True)],
        )

        proxy = services["docker-socket-proxy"]
        self.assertNotIn("ports", proxy)
        self.assertEqual(set(proxy["networks"]), {"docker-observability"})
        proxy_network = model["networks"]["docker-observability"]
        self.assertTrue(proxy_network["internal"])
        self.assertEqual(proxy_network.get("ipam", {}), {})
        network_members = {
            service_name
            for service_name, service in services.items()
            if "docker-observability" in service.get("networks", {})
        }
        self.assertEqual(network_members, {"alloy", "docker-socket-proxy"})
        self.assertEqual(
            proxy["image"],
            "tecnativa/docker-socket-proxy:v0.4.2@"
            "sha256:1f3a6f303320723d199d2316a3e82b2e2685d86c275d5e3deeaf182573b47476",
        )
        self.assertEqual(
            proxy["environment"],
            {
                "LOG_LEVEL": "warning",
            },
        )
        policy_mounts = [
            volume
            for volume in proxy["volumes"]
            if volume["target"] == "/usr/local/etc/haproxy/haproxy.cfg.template"
        ]
        self.assertEqual(len(policy_mounts), 1)
        self.assertEqual(
            Path(policy_mounts[0]["source"]),
            ROOT / "infra" / "docker-socket-proxy" / "haproxy.cfg.template",
        )
        self.assertTrue(policy_mounts[0]["read_only"])
        self.assertTrue(proxy["read_only"])
        self.assertEqual(proxy["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", proxy["security_opt"])
        self.assertEqual(
            set(proxy["tmpfs"]),
            {
                "/run:rw,nosuid,nodev,noexec,size=1m,mode=0755",
                "/tmp:rw,nosuid,nodev,noexec,size=8m,mode=1777",
            },
        )
        self.assertEqual(
            services["alloy"]["depends_on"]["docker-socket-proxy"]["condition"],
            "service_healthy",
        )
        self.assertEqual(
            services["alloy"]["environment"]["PYPI_COMPOSE_PROJECT"],
            "pypi-change-intelligence",
        )
        isolated_project = "pypi-fixture-smoke-" + "0123456789abcdef"
        isolated_model = self._load_compose_model({"COMPOSE_PROJECT_NAME": isolated_project})
        self.assertEqual(
            isolated_model["services"]["alloy"]["environment"]["PYPI_COMPOSE_PROJECT"],
            isolated_project,
        )
        self.assertEqual(
            isolated_model["networks"]["docker-observability"]["name"],
            f"{isolated_project}_docker-observability",
        )

    def test_all_topics_are_created_explicitly(self) -> None:
        script = (ROOT / "infra" / "redpanda" / "create-topics.sh").read_text()
        for topic in (
            "pypi.releases.v1",
            "pypi.ingest-failures.v1",
            "pypi.findings.v1",
            "pypi.failures.v1",
            "otel-traces",
        ):
            self.assertIn(topic, script)
        self.assertIn("--if-not-exists", script)
        self.assertNotIn("|| true", script)
        self.assertIn('-X "brokers=${BROKERS}"', script)
        self.assertNotIn("--brokers", script)
        self.assertIn(
            '--set "max.message.bytes=1048576"',
            script,
        )

        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertIn("rpk cluster health -X brokers=localhost:9092", compose)
        self.assertNotIn("rpk cluster health --brokers", compose)

    def test_source_dedupe_is_explicitly_process_local(self) -> None:
        for filename in (
            "source-fixture.yaml",
            "source-demo-fixture.yaml",
            "source-live.yaml",
            "source-history.yaml",
        ):
            source = (ROOT / "config" / "connect" / filename).read_text()
            self.assertIn("memory:", source)
            self.assertIn('compaction_interval: ""', source)
            self.assertNotIn("pypi.ingest-dedupe.v1", source)

    def test_default_fixture_emits_each_bounded_document_once(self) -> None:
        fixture = self._load_yaml(ROOT / "config" / "connect" / "source-fixture.yaml")
        generate = fixture["input"]["generate"]
        self.assertEqual(generate["count"], 0)
        self.assertEqual(generate["mapping"], 'from "/config/fixture-once.blobl"')
        self.assertTrue((ROOT / "config" / "connect" / "fixture-once.blobl").is_file())
        fixture_mapping = (ROOT / "config" / "connect" / "fixture-once.blobl").read_text()
        self.assertIn("pypi-updates.xml", fixture_mapping)
        self.assertIn("malformed-pypi-updates.xml", fixture_mapping)
        self.assertIn(
            "blobl -f /config/fixture-once.blobl",
            (ROOT / "infra" / "connect" / "lint-configs.sh").read_text(),
        )

        compose = self._load_yaml(ROOT / "docker-compose.yml")
        source_command = compose["services"]["connect-source"]["command"]
        self.assertIn("/config/${SOURCE_CONFIG:-source-fixture.yaml}", source_command)
        self.assertEqual(
            compose["services"]["api"]["environment"]["SOURCE_CONFIG"],
            "${SOURCE_CONFIG:-source-fixture.yaml}",
        )

    def test_history_configuration_precedes_fetch_and_ingestion(self) -> None:
        history = (ROOT / "config" / "connect" / "source-history.yaml").read_text()
        source_resources = (ROOT / "config" / "connect" / "source-resources.yaml").read_text()
        self.assertIn(
            'https://pypi.org/rss/project/${! meta("history_package") }/releases.xml',
            history,
        )
        self.assertIn('env("HISTORY_RELEASES_PER_PACKAGE")', source_resources)
        self.assertIn("interval: 24h", history)

        history_config = self._load_yaml(ROOT / "config" / "connect" / "source-history.yaml")
        processors = history_config["pipeline"]["processors"]
        self.assertEqual(processors[1]["resource"], "source_validate_history_configuration")
        self.assertIn("try", processors[2])
        self.assertEqual(processors[2]["try"][-1]["resource"], "source_ingestion_pipeline")

    def test_source_configs_share_strict_tested_ingestion_resources(self) -> None:
        resources = (ROOT / "config" / "connect" / "source-resources.yaml").read_text()
        self.assertIn("label: source_ingestion_pipeline", resources)
        self.assertIn("label: source_build_ingest_failure", resources)
        self.assertIn("try_catch:", resources)

        for filename in (
            "source-fixture.yaml",
            "source-demo-fixture.yaml",
            "source-live.yaml",
            "source-history.yaml",
        ):
            source = (ROOT / "config" / "connect" / filename).read_text()
            self.assertIn("error_handling:\n  strict: true", source)
            self.assertIn("- resource: source_ingestion_pipeline", source)

        connect_tests = ROOT / "tests" / "connect" / "source-fixture_benthos_test.yaml"
        self.assertTrue(connect_tests.exists())
        runner = (ROOT / "infra" / "connect" / "lint-configs.sh").read_text()
        self.assertIn("source-fixture_benthos_test.yaml", runner)
        self.assertRegex(runner, r'\$image" test ')

    def test_source_outputs_share_one_indefinite_acknowledgement_boundary(self) -> None:
        for filename in (
            "source-fixture.yaml",
            "source-demo-fixture.yaml",
            "source-live.yaml",
            "source-history.yaml",
        ):
            config = self._load_yaml(ROOT / "config" / "connect" / filename)
            retry = config["output"]["retry"]
            self.assertEqual(retry["max_retries"], 0)
            self.assertEqual(
                retry["backoff"],
                {
                    "initial_interval": "500ms",
                    "max_interval": "30s",
                    "max_elapsed_time": "0s",
                },
            )

            redpanda = retry["output"]["redpanda"]
            self.assertEqual(redpanda["topic"], '${! metadata("target_topic") }')
            self.assertEqual(redpanda["key"], '${! metadata("kafka_key") }')
            self.assertTrue(redpanda["idempotent_write"])
            self.assertEqual(redpanda["acks"], "all")
            self.assertEqual(redpanda["max_in_flight_requests"], 1)
            self.assertEqual(redpanda["record_retries"], 0)
            self.assertEqual(redpanda["record_delivery_timeout"], "0s")

            input_config = next(iter(config["input"].values()))
            self.assertTrue(input_config["auto_replay_nacks"])

    def test_output_retry_does_not_reenter_relevance_or_hide_schema_errors(
        self,
    ) -> None:
        resources = self._load_yaml(ROOT / "config" / "connect" / "source-resources.yaml")
        pipeline = next(
            resource
            for resource in resources["processor_resources"]
            if resource["label"] == "source_ingestion_pipeline"
        )
        processors = pipeline["switch"][0]["processors"]
        labels = [processor.get("label") for processor in processors]
        self.assertLess(
            labels.index("validate_release_candidate_contract"),
            labels.index("finish_relevance_or_propagate_system_error"),
        )

        resource_text = (ROOT / "config" / "connect" / "source-resources.yaml").read_text()
        self.assertEqual(resource_text.count("deleted()"), 1)
        self.assertIn('meta("relevance_decision") == "irrelevant"', resource_text)
        self.assertNotIn("retry:", resource_text)

        native_tests = (ROOT / "tests" / "connect" / "sink_benthos_test.yaml").read_text()
        self.assertIn("invalid fingerprinted ingestion envelope remains errored", native_tests)
        self.assertIn("errored()", native_tests)

    def test_database_has_attempt_and_trace_idempotency_boundaries(self) -> None:
        migration = (ROOT / "db" / "migrations" / "0001_initial.sql").read_text()
        self.assertIn("UNIQUE (event_key, analysis_version)", migration)
        self.assertIn("CREATE TABLE IF NOT EXISTS analysis_attempts", migration)
        self.assertIn("CREATE TABLE IF NOT EXISTS model_calls", migration)
        self.assertIn("analysis_trace_id char(32)", migration)
        self.assertIn("grafana_reader", migration)
        self.assertIn("request_sha256 char(71)", migration)
        self.assertIn("^sha256:[0-9a-f]{64}$", migration)
        self.assertIn("failure_id uuid", migration)
        self.assertIn("cache_write_tokens integer NOT NULL DEFAULT 0", migration)
        self.assertIn("materiality_decision text", migration)
        self.assertIn("analysis_method IN ('deterministic', 'model_assisted')", migration)
        self.assertIn("processing_priority IN ('high', 'medium', 'low', 'skip')", migration)
        self.assertNotIn("RENAME COLUMN", migration)
        self.assertIn("'materiality_assessment'", migration)
        self.assertIn("'applicability_assessment'", migration)
        self.assertIn("'applicability_correction'", migration)
        self.assertIn("'customer_impact_summary'", migration)
        self.assertEqual(
            [path.name for path in (ROOT / "db" / "migrations").glob("*.sql")],
            ["0001_initial.sql"],
        )

    def test_docker_context_excludes_local_dependency_trees(self) -> None:
        dockerignore = (ROOT / ".dockerignore").read_text()
        self.assertIn("**/node_modules", dockerignore)
        self.assertIn("**/.venv", dockerignore)

    def test_sink_persists_model_span_and_commit_evidence(self) -> None:
        sink = (ROOT / "config" / "connect" / "sink.yaml").read_text()
        self.assertIn("call->>'span_id'", sink)
        self.assertEqual(sink.count("cache_write_tokens"), 8)
        self.assertEqual(
            sink.count("call->'usage'->>'cache_write_tokens'"),
            2,
        )
        self.assertIn('"detail": "commit_proven_by_persisted_row"', sink)
        self.assertIn("this.payload.model_calls.or([]).format_json()", sink)
        self.assertIn("this.payload.source_event != null", sink)
        self.assertEqual(sink.count("CASE WHEN jsonb_typeof($3::jsonb) = 'array'"), 2)
        self.assertIn("pypi_sink_invalid_terminal_total", sink)
        self.assertIn('root = throw("unsupported terminal schema_version")', sink)
        self.assertNotIn("root = deleted()", sink)

    def test_worker_health_and_metrics_are_wired_into_smoke(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text()
        smoke = (ROOT / "infra" / "smoke.sh").read_text()
        self.assertIn("http://localhost:8090/ready", compose)
        self.assertIn("compose_url reasoning-worker 8090", smoke)
        self.assertIn("compose_url reasoning-worker 8001", smoke)
        self.assertIn("compose_url connect-trace-bridge 4197", smoke)

    def test_worker_shutdown_keeps_the_acknowledgement_boundary(self) -> None:
        compose = load_yaml_document(ROOT / "docker-compose.yml")
        self.assertEqual(
            compose["services"]["reasoning-worker"]["stop_grace_period"],
            "60s",
        )

        app = (ROOT / "services" / "reasoning" / "src" / "reasoning_worker" / "app.py").read_text()
        self.assertIn("signal.signal(signal.SIGTERM, stop)", app)
        self.assertIn("signal.signal(signal.SIGINT, stop)", app)
        cleanup = app.split("finally:", 1)[1]
        self.assertLess(cleanup.index("_Health.ready = False"), cleanup.index("worker.close()"))
        self.assertLess(cleanup.index("worker.close()"), cleanup.index("consumer.close()"))

        runtime = (
            ROOT / "services" / "reasoning" / "src" / "reasoning_worker" / "runtime.py"
        ).read_text()
        self.assertLess(
            runtime.index("self.publisher.publish("),
            runtime.index("self.consumer.commit(record)"),
        )

    def test_smoke_proves_data_path_trace_and_zero_lag(self) -> None:
        smoke = (ROOT / "infra" / "smoke.sh").read_text()
        makefile = (ROOT / "Makefile").read_text()
        trace_verifier = (ROOT / "scripts" / "ci" / "tempo_trace.py").read_text()
        self.assertIn("python3 -m scripts.ci.isolated_smoke", makefile)
        self.assertIn('--project-name "${SMOKE_PROJECT_NAME}"', smoke)
        self.assertIn("--env-file /dev/null", smoke)
        self.assertNotIn("restart connect-source", smoke)
        self.assertNotIn("docker compose down", smoke)
        self.assertNotIn("compose logs", smoke)
        self.assertIn("analysis_attempts", smoke)
        self.assertIn("aa.finding_id is not null", smoke)
        self.assertIn("aa.outcome = 'publishable'", smoke)
        self.assertIn('attempt_outcome" != "publishable', smoke)
        self.assertIn("/api/findings/${finding_id}", smoke)
        self.assertIn("python3 -m scripts.ci.tempo_trace", smoke)
        self.assertIn('service == "reasoning-worker"', trace_verifier)
        self.assertIn('service == "connect-sink"', trace_verifier)
        self.assertIn('"pypi.link.type"', trace_verifier)
        self.assertIn("zero_observations=0", smoke)
        self.assertIn("zero_observations=$((zero_observations + 1))", smoke)
        self.assertIn('[ "$zero_observations" -ge 2 ]', smoke)
        self.assertIn("wait_for_deterministic_scenarios", smoke)
        self.assertIn("verify_deterministic_scenario_api", smoke)
        for scenario_id in (
            "pypi:boto3:1.40.0rc1",
            "pypi:urllib3:2.6.1",
            "pypi:urllib3:2.6.0",
            "pypi:cffi:2.1.0",
            "pypi:packaging:26.2",
            "pypi:pluggy:1.6.1",
        ):
            self.assertIn(scenario_id, smoke)
        self.assertIn("ar.analysis_method = 'deterministic'", smoke)
        self.assertIn("ar.model_calls = '[]'::jsonb", smoke)
        self.assertIn("wait_for_loki_log", smoke)
        self.assertIn("compose_project", smoke)
        self.assertIn("wait_for_prometheus_scrape", smoke)
        for group in (
            "pypi-reasoning-v1",
            "pypi-postgres-sink-v1",
            "pypi-connect-trace-bridge-v1",
        ):
            self.assertIn(f"wait_for_zero_lag {group}", smoke)

    def test_strict_trace_contract_is_isolated_from_reliability_scenarios(self) -> None:
        smoke = (ROOT / "infra" / "smoke.sh").read_text()

        self.assertIn("requires_full_observability=0", smoke)
        self.assertIn(
            "ingestion-relevance | end-to-end-trace) requires_full_observability=1",
            smoke,
        )
        strict_start = smoke.index('if [ "$requires_full_observability" -eq 1 ]; then')
        self.assertGreater(
            smoke.index('wait_for_trace "$tempo_url" "$trace_id"', strict_start),
            strict_start,
        )
        self.assertGreater(
            smoke.index('wait_for_loki_log "$loki_url"', strict_start),
            strict_start,
        )
        self.assertGreater(
            smoke.index('wait_for_prometheus_scrape "$prometheus_url"', strict_start),
            strict_start,
        )

        for scenario in (
            "duplicate-terminal-replay",
            "analysis-version-replay",
            "deterministic-finding-ui",
            "full-stack-drain",
            "worker-shutdown",
            "runtime-signals",
        ):
            self.assertNotIn(
                f"{scenario}) requires_full_observability=1",
                smoke,
            )

    def test_telemetry_dependencies_use_real_http_healthchecks(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text()
        probe_image = (ROOT / "infra" / "telemetry" / "Dockerfile").read_text()
        self.assertIn("busybox:1.37.0-uclibc", probe_image)
        for endpoint in (
            "http://localhost:12345/-/ready",
            "http://localhost:3200/ready",
            "http://localhost:3100/ready",
            "http://localhost:9090/-/ready",
            "http://localhost:3000/api/health",
        ):
            self.assertIn(endpoint, compose)
        self.assertGreaterEqual(compose.count("condition: service_healthy"), 8)

    def test_fixture_rss_and_evidence_history_cover_monitored_packages(self) -> None:
        history = json.loads((ROOT / "data" / "fixtures" / "package-history.json").read_text())
        scenarios = json.loads(
            (ROOT / "data" / "fixtures" / "deterministic-scenarios.json").read_text()
        )["scenarios"]
        fixture_names = {entry["package"] for entry in history["events"]}
        monitored = set(
            json.loads((ROOT / "config" / "monitored-packages.json").read_text())["packages"]
        )
        history_keys = {
            f"https://pypi.org/project/{entry['package']}/{entry['version']}/"
            for entry in history["events"]
        }
        items = ET.parse(ROOT / "data" / "fixtures" / "pypi-updates.xml").findall("./channel/item")
        fixture_links = {item.findtext("link") for item in items}

        self.assertEqual(len(history["events"]), 24)
        self.assertEqual(len(fixture_names), 13)
        self.assertTrue(fixture_names.issubset(monitored))
        self.assertTrue(history_keys.issubset(fixture_links))
        self.assertEqual(
            {scenario["id"] for scenario in scenarios},
            {"R01", "R02", "R03", "R04", "R05", "R06"},
        )
        self.assertTrue(
            {scenario["event_key"].removeprefix("pypi:") for scenario in scenarios}.issubset(
                {f"{entry['package']}:{entry['version']}" for entry in history["events"]}
            )
        )
        self.assertIn("https://pypi.org/project/unmonitored-example/1.0.0/", fixture_links)
        self.assertIn("https://example.invalid/not-pypi", fixture_links)
        duplicate_links = [
            item.findtext("link")
            for item in items
            if item.findtext("link") == "https://pypi.org/project/urllib3/2.6.0/"
        ]
        self.assertEqual(len(duplicate_links), 2)
        self.assertTrue((ROOT / "data" / "fixtures" / "malformed-pypi-updates.xml").is_file())


if __name__ == "__main__":
    unittest.main()
