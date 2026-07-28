from __future__ import annotations

import base64
import unittest

from scripts.ci.tempo_trace import inspect_trace


def _attribute(key: str, value: str) -> dict[str, object]:
    return {"key": key, "value": {"stringValue": value}}


def _trace_payload(
    *,
    include_sink: bool = True,
    include_source_link: bool = True,
    link_on_worker: bool = True,
) -> dict[str, object]:
    worker_link = {
        "traceId": "1" * 32,
        "spanId": "2" * 16,
        "attributes": [_attribute("pypi.link.type", "source_ingestion")],
    }
    batches: list[dict[str, object]] = [
        {
            "resource": {"attributes": [_attribute("service.name", "reasoning-worker")]},
            "scopeSpans": [
                {
                    "spans": [
                        {
                            "traceId": "a" * 32,
                            "spanId": "b" * 16,
                            "name": "process pypi.releases.v1",
                            "links": [worker_link]
                            if include_source_link and link_on_worker
                            else [],
                        }
                    ]
                }
            ],
        }
    ]
    if include_sink:
        batches.append(
            {
                "resource": {"attributes": [_attribute("service.name", "connect-sink")]},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "a" * 32,
                                "spanId": "c" * 16,
                                "parentSpanId": "b" * 16,
                                "name": "consume pypi.analysis.completed.v1",
                                "links": [],
                            }
                        ]
                    }
                ],
            }
        )
    if include_source_link and not link_on_worker:
        batches.append(
            {
                "resource": {"attributes": [_attribute("service.name", "unrelated-service")]},
                "scopeSpans": [{"spans": [{"links": [worker_link]}]}],
            }
        )
    return {"batches": batches}


class TempoTraceContractTests(unittest.TestCase):
    def test_complete_trace_requires_worker_sink_and_worker_source_link(self) -> None:
        observation = inspect_trace(_trace_payload(), "a" * 32)

        self.assertTrue(observation.complete)
        self.assertEqual(observation.marker, "tempo_trace_w1_s1_l1_p1")
        self.assertEqual(
            observation.services,
            ("connect-sink", "reasoning-worker"),
        )
        self.assertEqual(observation.span_count, 2)

    def test_missing_sink_is_reported_without_hiding_other_evidence(self) -> None:
        observation = inspect_trace(
            _trace_payload(include_sink=False),
            "a" * 32,
        )

        self.assertFalse(observation.complete)
        self.assertEqual(observation.marker, "tempo_trace_w1_s0_l1_p0")
        self.assertEqual(observation.worker_span_count, 1)
        self.assertEqual(observation.sink_span_count, 0)

    def test_source_link_must_belong_to_a_worker_span(self) -> None:
        observation = inspect_trace(
            _trace_payload(link_on_worker=False),
            "a" * 32,
        )

        self.assertFalse(observation.complete)
        self.assertEqual(observation.marker, "tempo_trace_w1_s1_l0_p1")

    def test_sink_span_must_be_parented_to_the_worker_span(self) -> None:
        payload = _trace_payload()
        batches = payload["batches"]
        assert isinstance(batches, list)
        sink_batch = batches[1]
        assert isinstance(sink_batch, dict)
        scope_spans = sink_batch["scopeSpans"]
        assert isinstance(scope_spans, list)
        scope = scope_spans[0]
        assert isinstance(scope, dict)
        spans = scope["spans"]
        assert isinstance(spans, list)
        span = spans[0]
        assert isinstance(span, dict)
        span["parentSpanId"] = "d" * 16

        observation = inspect_trace(payload, "a" * 32)

        self.assertFalse(observation.complete)
        self.assertEqual(observation.marker, "tempo_trace_w1_s1_l1_p0")
        self.assertEqual(observation.sink_parented_to_worker_count, 0)

    def test_foreign_trace_spans_do_not_satisfy_the_contract(self) -> None:
        payload = _trace_payload()
        batches = payload["batches"]
        assert isinstance(batches, list)
        sink_batch = batches[1]
        assert isinstance(sink_batch, dict)
        scope_spans = sink_batch["scopeSpans"]
        assert isinstance(scope_spans, list)
        scope = scope_spans[0]
        assert isinstance(scope, dict)
        spans = scope["spans"]
        assert isinstance(spans, list)
        span = spans[0]
        assert isinstance(span, dict)
        span["traceId"] = "f" * 32

        observation = inspect_trace(payload, "a" * 32)

        self.assertFalse(observation.complete)
        self.assertEqual(observation.marker, "tempo_trace_w1_s0_l1_p0")

    def test_otlp_base64_trace_and_span_ids_match_hex_application_identity(
        self,
    ) -> None:
        payload = _trace_payload()
        batches = payload["batches"]
        assert isinstance(batches, list)
        for batch in batches:
            assert isinstance(batch, dict)
            scope_spans = batch["scopeSpans"]
            assert isinstance(scope_spans, list)
            scope = scope_spans[0]
            assert isinstance(scope, dict)
            spans = scope["spans"]
            assert isinstance(spans, list)
            span = spans[0]
            assert isinstance(span, dict)
            span["traceId"] = base64.b64encode(bytes.fromhex("a" * 32)).decode()
            span_id = span.get("spanId")
            if isinstance(span_id, str):
                span["spanId"] = base64.b64encode(bytes.fromhex(span_id)).decode()
            parent_span_id = span.get("parentSpanId")
            if isinstance(parent_span_id, str):
                span["parentSpanId"] = base64.b64encode(bytes.fromhex(parent_span_id)).decode()

        observation = inspect_trace(payload, "a" * 32)

        self.assertTrue(observation.complete)
        self.assertEqual(observation.sink_parented_to_worker_count, 1)


if __name__ == "__main__":
    unittest.main()
