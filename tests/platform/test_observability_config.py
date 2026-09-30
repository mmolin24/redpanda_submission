from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.platform.compose_support import load_yaml_document

ROOT = Path(__file__).resolve().parents[2]


def dictionaries(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from dictionaries(child)
    elif isinstance(value, list):
        for child in value:
            yield from dictionaries(child)


class ObservabilityConfigTests(unittest.TestCase):
    def test_connect_uses_free_redpanda_tracer_and_w3c_carrier(self) -> None:
        for filename in (
            "source-fixture.yaml",
            "source-live.yaml",
            "source-history.yaml",
            "sink.yaml",
        ):
            text = (ROOT / "config" / "connect" / filename).read_text()
            self.assertIn("tracer:\n  redpanda:", text)
            self.assertNotIn("open_telemetry_collector", text)
            self.assertIn("topic: otel-traces", text)
        source = (ROOT / "config" / "connect" / "source-fixture.yaml").read_text()
        sink = (ROOT / "config" / "connect" / "sink.yaml").read_text()
        self.assertIn("traceparent|tracestate", source)
        self.assertIn("inject_tracing_map: 'meta = @.merge(this)'", source)
        self.assertNotIn("inject_tracing_map: 'root = tracing_span()'", source)
        self.assertIn("root.traceparent = @traceparent", sink)
        self.assertIn("root.tracestate = @tracestate.or(deleted())", sink)
        self.assertIn("max_yield_batch_bytes: 1B", sink)

    def test_trace_stops_at_postgres_boundary(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text()
        alloy = (ROOT / "infra" / "alloy" / "config.alloy").read_text()
        self.assertNotIn(
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            compose.split("  api:", 1)[1].split("  web:", 1)[0],
        )
        self.assertNotIn("faro", alloy.lower())
        self.assertNotIn("browser", alloy.lower())
        self.assertNotIn("otelcol.receiver.kafka", alloy)
        bridge = (ROOT / "config" / "connect" / "trace-bridge.yaml").read_text()
        self.assertIn('"resourceSpans"', bridge)
        self.assertIn('"traceId"', bridge)
        self.assertIn('"spanId"', bridge)
        self.assertIn('"parentSpanId"', bridge)
        self.assertNotIn('"resource_spans"', bridge)
        self.assertIn("http://alloy:4318/v1/traces", bridge)

    def test_dashboards_are_provisioned_and_high_cardinality_ids_are_not_prom_labels(
        self,
    ) -> None:
        dashboards = list((ROOT / "infra" / "grafana" / "dashboards").glob("*.json"))
        self.assertGreaterEqual(len(dashboards), 4)
        for dashboard in dashboards:
            payload = json.loads(dashboard.read_text())
            self.assertIn("uid", payload)
            self.assertIn("title", payload)

        prometheus = (ROOT / "infra" / "prometheus" / "prometheus.yaml").read_text()
        for forbidden in (
            "event_key",
            "finding_id",
            "analysis_trace_id",
            "openai_request_id",
        ):
            self.assertNotIn(f"{forbidden}:", prometheus)

        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertIn('GF_USERS_VIEWERS_CAN_EDIT: "true"', compose)
        datasources = (
            ROOT / "infra" / "grafana" / "provisioning" / "datasources" / "datasources.yaml"
        ).read_text()
        self.assertEqual(datasources.count("database: $POSTGRES_DB"), 2)

    def test_full_model_payloads_remain_in_postgres_not_traces_or_logs(self) -> None:
        migration = (ROOT / "db" / "migrations" / "0001_initial.sql").read_text()
        self.assertIn("request_payload jsonb", migration)
        self.assertIn("response_payload jsonb", migration)
        alloy = (ROOT / "infra" / "alloy" / "config.alloy").read_text()
        self.assertNotIn("request_payload", alloy)
        self.assertNotIn("response_payload", alloy)

    def test_alloy_uses_the_restricted_docker_proxy_for_discovery_and_logs(
        self,
    ) -> None:
        alloy = (ROOT / "infra" / "alloy" / "config.alloy").read_text()
        self.assertEqual(
            alloy.count('host             = "http://docker-socket-proxy:2375"'),
            1,
        )
        self.assertEqual(
            alloy.count('host       = "http://docker-socket-proxy:2375"'),
            1,
        )
        self.assertNotIn("unix:///var/run/docker.sock", alloy)
        self.assertIn(
            """  filter {
    name   = "label"
    values = ["com.docker.compose.project=" + sys.env("PYPI_COMPOSE_PROJECT")]
  }""",
            alloy,
        )
        self.assertIn(
            """  rule {
    source_labels = ["__meta_docker_container_label_com_docker_compose_project"]
    regex         = sys.env("PYPI_COMPOSE_PROJECT")
    action        = "keep"
  }""",
            alloy,
        )
        self.assertIn(
            """  rule {
    source_labels = ["__meta_docker_container_label_com_docker_compose_service"]
    regex         = "docker-socket-proxy"
    action        = "drop"
  }""",
            alloy,
        )
        self.assertIn(
            """  rule {
    source_labels = ["__meta_docker_container_label_com_docker_compose_service"]
    target_label  = "service"
  }""",
            alloy,
        )
        self.assertNotIn("__meta_docker_container_name", alloy)

    def test_docker_proxy_policy_allows_only_required_read_paths(self) -> None:
        policy = (ROOT / "infra" / "docker-socket-proxy" / "haproxy.cfg.template").read_text()
        path_patterns = {
            line.split(" path_reg -i ", 1)[1]
            for line in policy.splitlines()
            if " path_reg -i " in line
        }
        self.assertEqual(
            path_patterns,
            {
                "^(/v[0-9.]+)?/_ping$",
                "^(/v[0-9.]+)?/version$",
                "^(/v[0-9.]+)?/containers/json$",
                "^(/v[0-9.]+)?/containers/[a-zA-Z0-9_.-]+/json$",
                "^(/v[0-9.]+)?/containers/[a-zA-Z0-9_.-]+/logs$",
                "^(/v[0-9.]+)?/networks$",
            },
        )
        self.assertIn("acl allowed_method method GET HEAD", policy)
        self.assertIn("http-request deny unless allowed_method", policy)
        self.assertNotIn("env(CONTAINERS)", policy)
        self.assertNotIn("/archive", policy)
        self.assertNotIn("/export", policy)

    def test_model_capture_status_stays_visible_without_payload_content(
        self,
    ) -> None:
        dashboard = json.loads(
            (ROOT / "infra" / "grafana" / "dashboards" / "reasoning-openai.json").read_text()
        )
        payload_panel = next(panel for panel in dashboard["panels"] if panel["id"] == 6)
        query = payload_panel["targets"][0]["rawSql"]
        self.assertIn("capture_status", query)
        self.assertIn("model_call_summaries", query)
        self.assertNotIn("request_payload", query)
        self.assertNotIn("response_payload", query)
        self.assertIn(
            "payload content remains restricted",
            payload_panel["description"],
        )

        sink = (ROOT / "config" / "connect" / "sink.yaml").read_text()
        self.assertEqual(
            sink.count("nullif(call->'request_payload', 'null'::jsonb)"),
            2,
        )
        self.assertEqual(
            sink.count("nullif(call->'response_payload', 'null'::jsonb)"),
            2,
        )

    def test_lag_alerts_and_worker_dashboards_use_exported_metrics(self) -> None:
        alerts = (ROOT / "infra" / "prometheus" / "alerts.yaml").read_text()
        topics = (ROOT / "infra" / "redpanda" / "create-topics.sh").read_text()
        pipeline = (
            ROOT / "infra" / "grafana" / "dashboards" / "pipeline-overview.json"
        ).read_text()
        reasoning = (
            ROOT / "infra" / "grafana" / "dashboards" / "reasoning-openai.json"
        ).read_text()
        self.assertIn("enable_consumer_group_metrics", topics)
        self.assertIn("redpanda_kafka_consumer_group_lag_sum", alerts)
        self.assertIn('job="redpanda"', alerts)
        self.assertIn("pypi_reasoning_messages_total", pipeline)
        self.assertIn("output_error", pipeline)
        self.assertIn("pypi_reasoning_model_calls_total", reasoning)
        self.assertIn("pypi_reasoning_model_tokens_total", reasoning)
        self.assertIn("cached_input_tokens", reasoning)
        self.assertIn("cache_write_tokens", reasoning)

    def test_ingestion_dlq_metrics_have_exact_names_and_low_cardinality_labels(
        self,
    ) -> None:
        resources = load_yaml_document(ROOT / "config" / "connect" / "source-resources.yaml")
        metric_configs = [item["metric"] for item in dictionaries(resources) if "metric" in item]
        metrics_by_name = {metric["name"]: metric for metric in metric_configs}

        for name in (
            "pypi_ingest_dlq_total",
            "pypi_ingest_snapshot_truncated_total",
        ):
            self.assertIn(name, metrics_by_name)
            self.assertEqual(set(metrics_by_name[name]["labels"]), {"error_class"})

        self.assertIn("pypi_source_delivery_ready_total", metrics_by_name)
        self.assertEqual(metrics_by_name["pypi_source_delivery_ready_total"].get("labels", {}), {})

        forbidden = {
            "failure_id",
            "event_key",
            "package",
            "fingerprint",
            "failure_fingerprint",
            "trace_id",
            "analysis_trace_id",
        }
        for metric in metric_configs:
            self.assertTrue(forbidden.isdisjoint(metric.get("labels", {})))

        source_text = (ROOT / "config" / "connect" / "source-resources.yaml").read_text()
        self.assertLess(
            source_text.index("label: validate_ingest_failure_contract"),
            source_text.index("name: pypi_ingest_dlq_total"),
        )
        self.assertLess(
            source_text.index("label: in_process_release_dedupe"),
            source_text.index("name: pypi_source_delivery_ready_total"),
        )
        for source_mode in (
            "source-fixture.yaml",
            "source-history.yaml",
            "source-live.yaml",
        ):
            source_config = load_yaml_document(ROOT / "config" / "connect" / source_mode)
            self.assertEqual(
                source_config["output"]["retry"]["output"]["label"],
                "source_redpanda_delivery",
            )
            output_section = (
                (ROOT / "config" / "connect" / source_mode).read_text().split("output:", 1)[1]
            )
            self.assertNotIn("pypi_ingest_dlq_total", output_section)

    def test_failure_alerts_distinguish_dlq_publication_from_backpressure(self) -> None:
        alerts = load_yaml_document(ROOT / "infra" / "prometheus" / "alerts.yaml")
        rules = {rule["alert"]: rule for group in alerts["groups"] for rule in group["rules"]}

        dlq = rules["IngestionDlqActivity"]
        self.assertEqual(dlq["labels"]["severity"], "warning")
        self.assertIn('increase(pypi_ingest_dlq_total{job="connect-source"}[5m]) > 0', dlq["expr"])
        self.assertEqual(
            dlq["annotations"]["summary"],
            "A valid source failure entered the DLQ publication path",
        )
        self.assertIn(
            "Delivery and persistence are not yet confirmed",
            dlq["annotations"]["description"],
        )

        output_errors = rules["SourceOutputErrorsPersistent"]
        self.assertEqual(output_errors["for"], "2m")
        self.assertEqual(output_errors["labels"]["severity"], "warning")
        self.assertIn(
            'output_error{job="connect-source",label="source_redpanda_delivery"}',
            output_errors["expr"],
        )
        self.assertIn(
            'pypi_source_delivery_ready_total{job="connect-source"}',
            output_errors["expr"],
        )
        self.assertIn(
            'output_sent{job="connect-source",label="source_redpanda_delivery"}',
            output_errors["expr"],
        )

        connection = rules["SourceOutputConnectionFailures"]
        self.assertEqual(connection["labels"]["severity"], "critical")
        self.assertIn(
            'output_connection_failed{job="connect-source",label="source_redpanda_delivery"}',
            connection["expr"],
        )
        self.assertIn('pypi_source_delivery_ready_total{job="connect-source"}', connection["expr"])
        self.assertIn(
            'output_sent{job="connect-source",label="source_redpanda_delivery"}',
            connection["expr"],
        )
        self.assertIn('up{job="connect-source"}', connection["expr"])
        self.assertIn('up{job="redpanda"}', connection["expr"])
        self.assertIn("== 1", connection["expr"])
        self.assertIn("label_replace", connection["expr"])

        reasoning = rules["ReasoningBackpressure"]
        self.assertEqual(reasoning["labels"]["severity"], "critical")
        self.assertIn(
            'pypi_reasoning_backpressure_total{job="reasoning-worker"}',
            reasoning["expr"],
        )
        self.assertIn(
            "source offset remains uncommitted",
            reasoning["annotations"]["description"].lower(),
        )

        oversize = rules["ReasoningTerminalOversize"]
        self.assertEqual(oversize["labels"]["severity"], "warning")
        self.assertEqual(
            oversize["expr"],
            'increase(pypi_reasoning_terminal_oversize_total{job="reasoning-worker"}[5m]) > 0',
        )
        self.assertIn(
            "broker acknowledged",
            oversize["annotations"]["description"].lower(),
        )
        self.assertIn(
            "persistence is not yet confirmed",
            oversize["annotations"]["description"].lower(),
        )

        retained = {
            "SourceConnectDown",
            "SinkConnectDown",
            "PostgresDown",
            "WorkerConsumerLagHigh",
            "SinkConsumerLagHigh",
            "TelemetryRejected",
        }
        self.assertTrue(retained.issubset(rules))

    def test_pipeline_dashboard_separates_oversize_dlq_from_backpressure(self) -> None:
        dashboard = json.loads(
            (ROOT / "infra" / "grafana" / "dashboards" / "pipeline-overview.json").read_text()
        )
        panels = {panel["title"]: panel for panel in dashboard["panels"]}
        oversize = panels["Oversized terminals sent to failure topic"]
        oversize_expr = oversize["targets"][0]["expr"]
        self.assertEqual(
            oversize_expr,
            'sum by (original_type) (increase(pypi_reasoning_terminal_oversize_total{job="reasoning-worker"}[5m]))',
        )
        self.assertEqual(
            oversize["targets"][0]["legendFormat"],
            "{{original_type}}",
        )
        self.assertIn("broker acknowledgement", oversize["description"])
        self.assertIn("persistence", oversize["description"])
        self.assertNotIn("pypi_reasoning_backpressure_total", oversize_expr)

        backpressure = panels["Unacknowledged reasoning backpressure"]
        backpressure_expr = backpressure["targets"][0]["expr"]
        self.assertIn("pypi_reasoning_backpressure_total", backpressure_expr)
        self.assertNotIn("pypi_reasoning_terminal_oversize_total", backpressure_expr)
        self.assertIn("uncommitted", backpressure["description"])

    def test_failure_dashboard_covers_occurrences_groups_evidence_and_lineage(
        self,
    ) -> None:
        dashboard = json.loads(
            (ROOT / "infra" / "grafana" / "dashboards" / "failures-replay.json").read_text()
        )
        self.assertEqual(dashboard["title"], "PyPI Failure Operations")
        self.assertNotIn("acknowledged", dashboard["description"].lower())
        self.assertIn("publication", dashboard["description"])
        self.assertIn("persistence", dashboard["description"])
        self.assertIn("backpressure", dashboard["description"])
        panels = {panel["title"]: panel for panel in dashboard["panels"]}
        expected = {
            "Unresolved failure occurrences",
            "Unresolved fingerprint groups",
            "Failure occurrence rate by stage and class",
            "Truncated evidence occurrences",
            "DLQ routing decisions (5m)",
            "Persisted failure occurrences",
            "Source infrastructure backpressure (5m)",
        }
        self.assertTrue(expected.issubset(panels))

        occurrences = json.dumps(panels["Unresolved failure occurrences"])
        for field in (
            "failure_fingerprint",
            "source_topic",
            "source_partition",
            "source_offset",
            "analysis_trace_id",
        ):
            self.assertIn(field, occurrences)
        self.assertIn("tempo", occurrences)
        self.assertIn("failure_occurrence_summaries", occurrences)
        self.assertNotIn("raw_failure", occurrences)
        self.assertNotIn("snapshot_base64", occurrences)

        groups_sql = panels["Unresolved fingerprint groups"]["targets"][0]["rawSql"]
        for term in ("COUNT(*)", "MIN(first_failed_at)", "MAX(last_failed_at)"):
            self.assertIn(term, groups_sql)
        truncated_sql = panels["Truncated evidence occurrences"]["targets"][0]["rawSql"]
        self.assertIn("snapshot_truncated", truncated_sql)

        dlq_panel = panels["DLQ routing decisions (5m)"]
        dlq_expr = dlq_panel["targets"][0]["expr"]
        backpressure_expr = panels["Source infrastructure backpressure (5m)"]["targets"][0]["expr"]
        self.assertIn("pypi_ingest_dlq_total", dlq_expr)
        self.assertNotIn("output_error", dlq_expr)
        self.assertIn("before broker acknowledgement", dlq_panel["description"])
        self.assertIn("output_error", backpressure_expr)
        self.assertNotIn("pypi_ingest_dlq_total", backpressure_expr)
        backpressure_targets = panels["Source infrastructure backpressure (5m)"]["targets"]
        self.assertTrue(
            any(
                "pypi_source_delivery_ready_total" in target["expr"]
                for target in backpressure_targets
            )
        )
        self.assertTrue(
            any('up{job="redpanda"} == 0' in target["expr"] for target in backpressure_targets)
        )

        persisted_sql = panels["Persisted failure occurrences"]["targets"][0]["rawSql"]
        self.assertIn("failure_occurrence_summaries", persisted_sql)
        self.assertIn("Safely persisted", panels["Persisted failure occurrences"]["description"])
        self.assertIn(
            "durably stored and idempotently replayable",
            panels["Persisted failure occurrences"]["description"],
        )
        self.assertNotIn(
            "acknowledged",
            panels["Persisted failure occurrences"]["description"].lower(),
        )

        for panel in panels.values():
            for target in panel.get("targets", []):
                sql = target.get("rawSql", "")
                if sql:
                    self.assertNotIn("processing_failures", sql)

    def test_failure_observability_view_is_least_privilege(self) -> None:
        sql = (ROOT / "db" / "migrations" / "0001_initial.sql").read_text()
        self.assertIn("CREATE OR REPLACE VIEW failure_occurrence_summaries", sql)
        self.assertIn("payload @> '{\"truncated\": true}'::jsonb AS snapshot_truncated", sql)
        self.assertNotIn("GRANT SELECT ON processing_failures", sql)
        view_projection = sql.split("CREATE OR REPLACE VIEW failure_occurrence_summaries", 1)[
            1
        ].split("CREATE OR REPLACE VIEW pipeline_run_summaries", 1)[0]
        self.assertNotIn("raw_failure", view_projection)
        self.assertNotIn("snapshot_base64", view_projection)

    def test_grafana_queries_only_reviewed_database_views(self) -> None:
        reasoning_dashboard = json.loads(
            (ROOT / "infra" / "grafana" / "dashboards" / "reasoning-openai.json").read_text()
        )
        reasoning_sql = [
            target["rawSql"]
            for panel in reasoning_dashboard["panels"]
            for target in panel.get("targets", [])
            if "rawSql" in target
        ]
        self.assertTrue(reasoning_sql)
        for query in reasoning_sql:
            self.assertIn("model_call_summaries", query)
            self.assertNotIn("FROM model_calls", query)
            self.assertNotIn("request_payload", query)
            self.assertNotIn("response_payload", query)

        pipeline_dashboard = json.loads(
            (ROOT / "infra" / "grafana" / "dashboards" / "pipeline-overview.json").read_text()
        )
        pipeline_sql = [
            target["rawSql"]
            for panel in pipeline_dashboard["panels"]
            for target in panel.get("targets", [])
            if "rawSql" in target
        ]
        self.assertEqual(len(pipeline_sql), 1)
        self.assertIn("FROM pipeline_run_summaries", pipeline_sql[0])
        self.assertNotIn("FROM analysis_runs", pipeline_sql[0])

    def test_schema_baseline_revokes_raw_and_future_grafana_access(self) -> None:
        sql = (ROOT / "db" / "migrations" / "0001_initial.sql").read_text()
        self.assertIn("CREATE OR REPLACE VIEW model_call_summaries", sql)
        self.assertIn("CREATE OR REPLACE VIEW pipeline_run_summaries", sql)
        self.assertIn(
            "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public",
            sql,
        )
        self.assertIn(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public",
            sql,
        )
        self.assertIn(
            "REVOKE SELECT ON TABLES FROM grafana_reader",
            sql,
        )
        self.assertIn(
            "REVOKE TEMPORARY ON DATABASE",
            sql,
        )
        self.assertIn(
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
            sql,
        )
        model_projection = sql.split("CREATE OR REPLACE VIEW model_call_summaries", 1)[1].split(
            "DO $$", 1
        )[0]
        self.assertNotIn("openai_request_id", model_projection)
        self.assertNotIn("client_request_id", model_projection)
        self.assertNotIn("response_id", model_projection)

        initial_sql = (ROOT / "db" / "migrations" / "0001_initial.sql").read_text()
        self.assertNotIn(
            "GRANT SELECT ON finding_summaries, analysis_trace_summaries, model_calls",
            initial_sql,
        )
        self.assertNotIn(
            "GRANT SELECT ON TABLES TO grafana_reader",
            initial_sql,
        )
        self.assertNotIn(
            "GRANT SELECT ON finding_summaries",
            initial_sql,
        )

    def test_schema_baseline_hardens_grafana_reader(self) -> None:
        sql = (ROOT / "db" / "migrations" / "0001_initial.sql").read_text()
        self.assertIn(
            "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public",
            sql,
        )
        self.assertIn(
            "REVOKE SELECT ON TABLES FROM grafana_reader",
            sql,
        )
        self.assertIn("ALTER ROLE grafana_reader WITH", sql)
        self.assertIn("NOSUPERUSER", sql)
        self.assertIn("NOCREATEDB", sql)
        self.assertIn("NOCREATEROLE", sql)
        self.assertIn("NOREPLICATION", sql)
        self.assertIn("NOBYPASSRLS", sql)

        init_script = (ROOT / "infra" / "db" / "init.sh").read_text()
        self.assertIn("for migration in /migrations/*.sql", init_script)
        self.assertNotIn("security_preflight", init_script)

    def test_prometheus_rules_are_checked_by_the_pinned_image(self) -> None:
        makefile = (ROOT / "Makefile").read_text()
        check_runner = (ROOT / "scripts" / "check.sh").read_text()
        runner = ROOT / "infra" / "prometheus" / "check-config.sh"
        self.assertTrue(runner.exists())
        self.assertIn("scripts/check.sh", makefile)
        self.assertIn("infra/prometheus/check-config.sh", check_runner)
        runner_text = runner.read_text()
        self.assertIn("prom/prometheus:v3.12.0", runner_text)
        self.assertEqual(runner_text.count("--entrypoint promtool"), 3)
        self.assertIn('"${image}" check config', runner_text)
        self.assertIn('"${image}" check rules', runner_text)
        self.assertIn('"${image}" test rules', runner_text)
        self.assertTrue((ROOT / "infra" / "prometheus" / "alerts_test.yaml").exists())

        metrics_runner = ROOT / "infra" / "connect" / "test-source-metrics.sh"
        self.assertTrue(metrics_runner.exists())
        metrics_text = metrics_runner.read_text()
        self.assertIn("docker.redpanda.com/redpandadata/connect:4.99.0", metrics_text)
        self.assertIn("pypi_ingest_dlq_total", metrics_text)
        self.assertIn("pypi_ingest_snapshot_truncated_total", metrics_text)
        self.assertIn("output_error", metrics_text)
        self.assertIn("output_connection_failed", metrics_text)
        self.assertIn("output_connection_lost", metrics_text)
        self.assertIn("pypi_source_delivery_ready_total", metrics_text)
        self.assertIn('delivery_ready}" -eq 1', metrics_text)
        self.assertIn('output_sent}" -eq 0', metrics_text)
        self.assertIn("DLQ routing counter changed during output retry", metrics_text)
        self.assertNotIn("docker network create", metrics_text)
        self.assertIn("--network none", metrics_text)
        self.assertIn('--network "container:${redpanda_container}"', metrics_text)
        self.assertIn("SOURCE_METRICS_REDPANDA_BROKERS=localhost:9092", metrics_text)
        metrics_config = (ROOT / "tests" / "connect" / "source-metrics-test.yaml").read_text()
        self.assertIn(
            "${SOURCE_METRICS_REDPANDA_BROKERS:redpanda:9092}",
            metrics_config,
        )


if __name__ == "__main__":
    unittest.main()
