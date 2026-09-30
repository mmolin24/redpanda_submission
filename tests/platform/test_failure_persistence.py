from __future__ import annotations

import unittest
from pathlib import Path

from tests.platform.compose_support import load_yaml_document

ROOT = Path(__file__).resolve().parents[2]


class FailurePersistenceTests(unittest.TestCase):
    def test_schema_baseline_enforces_grouping_fingerprints(self) -> None:
        baseline_path = ROOT / "db" / "migrations" / "0001_initial.sql"
        self.assertTrue(baseline_path.exists())
        baseline = baseline_path.read_text()

        self.assertIn("failure_fingerprint char(71) NOT NULL", baseline)
        self.assertIn("btrim(failure_fingerprint) ~ '^sha256:[0-9a-f]{64}$'", baseline)
        self.assertIn("processing_failures_unresolved_fingerprint_time_idx", baseline)
        self.assertNotIn("UNIQUE (failure_fingerprint", baseline)

    def test_sink_projects_both_failure_topics_without_a_feedback_loop(self) -> None:
        sink = load_yaml_document(ROOT / "config" / "connect" / "sink.yaml")
        redpanda_input = sink["input"]["redpanda"]
        self.assertEqual(
            redpanda_input["topics"],
            ["pypi.findings.v1", "pypi.failures.v1", "pypi.ingest-failures.v1"],
        )
        self.assertTrue(redpanda_input["auto_replay_nacks"])
        self.assertTrue(sink["error_handling"]["strict"])

        validation_cases = sink["pipeline"]["processors"][0]["switch"]
        ingest_validation = next(
            case for case in validation_cases if "pypi.ingest-failures.v1" in case.get("check", "")
        )
        self.assertIn(
            'metadata("kafka_topic") == "pypi.ingest-failures.v1"',
            ingest_validation["check"],
        )
        self.assertEqual(
            ingest_validation["processors"][0]["json_schema"]["schema_path"],
            "file:///schemas/ingest-failure.schema.json",
        )

        output_switch = sink["output"]["switch"]
        self.assertTrue(output_switch["retry_until_success"])
        failure_case = next(
            case
            for case in output_switch["cases"]
            if case["check"] == 'this.schema_version == "failure.v1"'
        )
        sql_output = failure_case["output"]["sql_raw"]
        queries = sql_output["queries"]
        persistence_query = next(
            query for query in queries if "INSERT INTO processing_failures" in query["query"]
        )
        sql = persistence_query["query"]
        args = persistence_query["args_mapping"]

        self.assertIn("failure_fingerprint", sql)
        self.assertIn("this._lineage.source_topic", args)
        self.assertIn("ON CONFLICT (failure_id) DO UPDATE", sql)
        self.assertIn("failure_fingerprint = EXCLUDED.failure_fingerprint", sql)
        self.assertIn("this.failure_fingerprint", args)
        self.assertIn("this._lineage.raw_failure", args)
        self.assertNotIn('this.without("_lineage").format_json()', args)
        self.assertNotIn("redpanda", failure_case["output"])

        finding_case = next(
            case
            for case in output_switch["cases"]
            if case["check"] == 'this.schema_version == "finding.v1"'
        )
        finding_query = next(
            query
            for query in finding_case["output"]["sql_raw"]["queries"]
            if "INSERT INTO findings" in query["query"]
        )
        self.assertIn(
            "WHERE $10::boolean OR $11 = 'insufficient_evidence'",
            finding_query["query"],
        )
        self.assertIn("this.publishable, this.disposition", finding_query["args_mapping"])

        invalid_terminal = validation_cases[-1]
        invalid_terminal_mapping = invalid_terminal["processors"][-1]["mapping"]
        self.assertIn("throw(", invalid_terminal_mapping)
        self.assertNotIn("deleted()", invalid_terminal_mapping)

        lineage_mapping = sink["pipeline"]["processors"][1]["mapping"]
        self.assertIn("content().string()", lineage_mapping)
        self.assertIn('"raw_failure": $raw_failure', lineage_mapping)

    def test_sink_transaction_failure_remains_retryable_and_uncommitted(self) -> None:
        sink = load_yaml_document(ROOT / "config" / "connect" / "sink.yaml")
        redpanda_input = sink["input"]["redpanda"]
        output_switch = sink["output"]["switch"]
        failure_case = next(
            case
            for case in output_switch["cases"]
            if case["check"] == 'this.schema_version == "failure.v1"'
        )
        sql_output = failure_case["output"]["sql_raw"]

        self.assertTrue(redpanda_input["auto_replay_nacks"])
        self.assertGreater(len(sql_output["queries"]), 1)
        self.assertTrue(output_switch["retry_until_success"])
        self.assertNotIn("fallback", failure_case["output"])
        self.assertEqual(set(failure_case["output"]), {"sql_raw"})

    def test_connect_native_tests_cover_invalid_envelopes_and_immutable_wire_data(
        self,
    ) -> None:
        runner = (ROOT / "infra" / "connect" / "lint-configs.sh").read_text()
        native_test = ROOT / "tests" / "connect" / "sink_benthos_test.yaml"

        self.assertTrue(native_test.exists())
        self.assertIn("sink_benthos_test.yaml", runner)
        test_text = native_test.read_text()
        self.assertIn("invalid fingerprinted ingestion envelope remains errored", test_text)
        self.assertIn("unknown terminal schema remains errored", test_text)
        self.assertIn("current failure retains immutable wire JSON", test_text)


if __name__ == "__main__":
    unittest.main()
