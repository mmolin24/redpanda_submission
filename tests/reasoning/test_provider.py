from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from reasoning_worker.ids import sha256_json
from reasoning_worker.models import ServiceTier
from reasoning_worker.provider import (
    MATERIALITY_SCHEMA,
    DeterministicFakeProvider,
    ModelRequest,
    OpenAIResponsesProvider,
    ProviderExhausted,
)
from reasoning_worker.telemetry import WorkerTelemetry


class RateLimited(Exception):
    status_code = 429


class BadRequest(Exception):
    status_code = 400


class Responses:
    def __init__(self, parent):
        self.parent = parent

    def create(self, **body):
        self.parent.bodies.append(body)
        if self.parent.calls == 0:
            self.parent.calls += 1
            raise RateLimited("retry")
        self.parent.calls += 1
        return SimpleNamespace(
            id="resp_1",
            status="completed",
            model="gpt-5.6-sol-2026-07-01",
            service_tier="flex",
            output=[],
            output_text=json.dumps(
                {
                    "decision": "non_substantive",
                    "change_types": [],
                    "claims": [],
                    "missing_evidence": [],
                    "confidence": 0.9,
                }
            ),
            usage=SimpleNamespace(
                input_tokens=100,
                input_tokens_details=SimpleNamespace(cached_tokens=20, cache_write_tokens=30),
                output_tokens=10,
                output_tokens_details=SimpleNamespace(reasoning_tokens=2),
            ),
            incomplete_details=None,
            _request_id="req_1",
        )


class Client:
    def __init__(self):
        self.calls = 0
        self.bodies = []
        self.headers = []
        self.responses = Responses(self)

    def with_options(self, **options):
        self.headers.append(options)
        return self


def test_openai_adapter_owns_retry_ids_and_captures_visible_payload_only(
    monkeypatch,
):
    monkeypatch.setenv("DEPLOYMENT_ENV", "local")
    monkeypatch.setenv("OBS_CAPTURE_MODEL_PAYLOADS", "true")
    client = Client()
    sleeps = []
    provider = OpenAIResponsesProvider(
        client,
        sleep=sleeps.append,
        random_value=lambda: 0,
    )
    result = provider.complete(
        ModelRequest(
            purpose="materiality_assessment",
            instructions="Classify",
            model_input={"evidence": {"facts": []}},
            output_schema=MATERIALITY_SCHEMA,
            output_schema_name="materiality_assessment",
            service_tier=ServiceTier.FLEX,
        )
    )
    assert client.calls == 2
    assert len(sleeps) == 1
    assert client.headers[0]["max_retries"] == 0
    ids = [options["default_headers"]["X-Client-Request-Id"] for options in client.headers]
    assert len(set(ids)) == 2
    assert all(body["store"] is False for body in client.bodies)
    assert all(body["model"] == "gpt-5.6-sol" for body in client.bodies)
    assert result.call.usage.reasoning_tokens == 2
    assert result.call.usage.output_tokens == 10
    assert result.call.usage.cached_input_tokens == 20
    assert result.call.usage.cache_write_tokens == 30
    assert result.call.estimated_cost_usd == 0.00037375
    assert result.call.response_payload is not None
    assert result.call.request_payload is not None
    assert "reasoning" not in result.call.response_payload
    assert "authorization" not in str(result.call.request_payload).lower()
    assert result.call.request_payload == client.bodies[-1]
    assert result.call.request_sha256 == sha256_json(client.bodies[-1])


def test_openai_adapter_can_disable_prompt_cache_reads_and_writes(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_ENV", "local")
    monkeypatch.setenv("OBS_CAPTURE_MODEL_PAYLOADS", "true")
    client = Client()
    client.calls = 1
    provider = OpenAIResponsesProvider(
        client,
        prompt_cache_enabled=False,
    )

    result = provider.complete(
        ModelRequest(
            purpose="materiality_assessment",
            instructions="Classify",
            model_input={"evidence": {"facts": []}},
            output_schema=MATERIALITY_SCHEMA,
            output_schema_name="materiality_assessment",
        )
    )

    request = client.bodies[-1]
    assert request["prompt_cache_options"] == {"mode": "explicit"}
    assert "prompt_cache_breakpoint" not in str(request)
    assert result.call.request_payload == request


def test_stage_cache_marks_only_the_stable_prefix_and_versions_its_key():
    client = Client()
    client.calls = 1
    provider = OpenAIResponsesProvider(client)

    first = ModelRequest(
        purpose="materiality_correction",
        instructions="Stable materiality assessment policy",
        instruction_suffix="Repair this invalid result",
        cache_namespace="materiality",
        model_input={"release": "requests 2.33.0"},
        output_schema=MATERIALITY_SCHEMA,
        output_schema_name="materiality_assessment",
    )
    provider.complete(first)
    first_body = client.bodies[-1]

    second = ModelRequest(
        purpose="materiality_correction",
        instructions="Stable materiality assessment policy",
        instruction_suffix="Repair this invalid result",
        cache_namespace="materiality",
        model_input={"release": "urllib3 2.7.0"},
        output_schema=MATERIALITY_SCHEMA,
        output_schema_name="materiality_assessment",
    )
    provider.complete(second)
    second_body = client.bodies[-1]

    assert first_body["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    assert first_body["prompt_cache_key"] == second_body["prompt_cache_key"]
    assert first_body["prompt_cache_key"].startswith("pypi-materiality-")
    developer_content = first_body["input"][0]["content"]
    assert developer_content == [
        {
            "type": "input_text",
            "text": "Stable materiality assessment policy",
            "prompt_cache_breakpoint": {"mode": "explicit"},
        },
        {"type": "input_text", "text": "Repair this invalid result"},
    ]
    assert "requests 2.33.0" not in json.dumps(developer_content)
    assert "requests 2.33.0" in first_body["input"][1]["content"]
    assert first_body["input"][1] != second_body["input"][1]


def test_stage_cache_key_changes_with_the_stable_contract_not_fresh_evidence():
    client = Client()
    client.calls = 1
    provider = OpenAIResponsesProvider(client)

    def complete(*, instructions: str, schema_name: str, release: str):
        provider.complete(
            ModelRequest(
                purpose="applicability_assessment",
                instructions=instructions,
                cache_namespace="applicability",
                model_input={"release": release},
                output_schema=MATERIALITY_SCHEMA,
                output_schema_name=schema_name,
            )
        )
        return client.bodies[-1]["prompt_cache_key"]

    original = complete(
        instructions="Stable applicability assessment policy",
        schema_name="applicability_contract",
        release="requests 2.33.0",
    )
    new_evidence = complete(
        instructions="Stable applicability assessment policy",
        schema_name="applicability_contract",
        release="urllib3 2.7.0",
    )
    new_policy = complete(
        instructions="Changed applicability assessment policy",
        schema_name="applicability_contract",
        release="urllib3 2.7.0",
    )
    new_schema = complete(
        instructions="Stable applicability assessment policy",
        schema_name="applicability_contract_v2",
        release="urllib3 2.7.0",
    )

    assert original == new_evidence
    assert original != new_policy
    assert original != new_schema


def test_exhausted_provider_error_retains_sanitized_request_and_all_attempts(
    monkeypatch,
):
    monkeypatch.setenv("DEPLOYMENT_ENV", "local")
    monkeypatch.setenv("OBS_CAPTURE_MODEL_PAYLOADS", "true")
    client = Client()
    client.responses.create = lambda **_body: (_ for _ in ()).throw(RateLimited("retry"))
    provider = OpenAIResponsesProvider(
        client,
        max_attempts=2,
        sleep=lambda _seconds: None,
    )
    request = ModelRequest(
        purpose="materiality_assessment",
        instructions="Classify",
        model_input={"evidence": {"facts": []}},
        output_schema=MATERIALITY_SCHEMA,
        output_schema_name="materiality_assessment",
    )
    with pytest.raises(ProviderExhausted) as error:
        provider.complete(request)
    assert len(error.value.call.attempts) == 2
    assert error.value.retryable is True
    assert error.value.call.request_payload is not None
    assert error.value.call.request_payload["store"] is False
    assert error.value.call.response_payload is None


def test_non_retryable_provider_error_preserves_systemic_classification():
    client = Client()
    client.responses.create = lambda **_body: (_ for _ in ()).throw(BadRequest("invalid config"))
    provider = OpenAIResponsesProvider(client, max_attempts=3, sleep=lambda _seconds: None)

    with pytest.raises(ProviderExhausted) as error:
        provider.complete(
            ModelRequest(
                purpose="materiality_assessment",
                instructions="Classify",
                model_input={"evidence": {"facts": []}},
                output_schema=MATERIALITY_SCHEMA,
                output_schema_name="materiality_assessment",
            )
        )

    assert error.value.retryable is False
    assert len(error.value.call.attempts) == 1


def test_keyboard_interrupt_is_not_swallowed_or_retried():
    client = Client()
    client.responses.create = lambda **_body: (_ for _ in ()).throw(KeyboardInterrupt())
    provider = OpenAIResponsesProvider(client, sleep=lambda _seconds: None)
    with pytest.raises(KeyboardInterrupt):
        provider.complete(
            ModelRequest(
                purpose="materiality_assessment",
                instructions="Classify",
                model_input={"evidence": {"facts": []}},
                output_schema=MATERIALITY_SCHEMA,
                output_schema_name="materiality_assessment",
            )
        )
    assert client.calls == 0


def test_deterministic_fake_provider_bounds_its_local_delay():
    with pytest.raises(ValueError, match="delay"):
        DeterministicFakeProvider(delay_seconds=-1)
    with pytest.raises(ValueError, match="delay"):
        DeterministicFakeProvider(delay_seconds=31)


def test_provider_spans_never_export_raw_exception_text_or_stacktraces():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = WorkerTelemetry(tracer_provider.get_tracer("provider-sanitization-test"))
    client = Client()
    marker = "AUDIT_SECRET_MARKER"
    client.responses.create = lambda **_body: (_ for _ in ()).throw(BadRequest(marker))
    provider = OpenAIResponsesProvider(
        client,
        telemetry=telemetry,
        max_attempts=1,
    )

    with pytest.raises(ProviderExhausted):
        provider.complete(
            ModelRequest(
                purpose="materiality_assessment",
                instructions="Classify",
                model_input={"evidence": {"facts": []}},
                output_schema=MATERIALITY_SCHEMA,
                output_schema_name="materiality_assessment",
            )
        )

    spans = exporter.get_finished_spans()
    assert spans
    assert all(marker not in repr(span) for span in spans)
    assert all(marker not in repr(span.events) for span in spans)
    assert {
        span.attributes.get("error.type")
        for span in spans
        if span.attributes is not None and span.attributes.get("error.type")
    } == {"bad_request", "provider_exhausted"}


def test_model_call_persists_the_genai_span_id_and_metrics():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = WorkerTelemetry(tracer_provider.get_tracer("test"))
    client = Client()
    client.calls = 1
    result = OpenAIResponsesProvider(client, telemetry=telemetry).complete(
        ModelRequest(
            purpose="materiality_assessment",
            instructions="Classify",
            model_input={"evidence": {"facts": []}},
            output_schema=MATERIALITY_SCHEMA,
            output_schema_name="materiality_assessment",
        )
    )
    model_span = next(
        span
        for span in exporter.get_finished_spans()
        if span.name.startswith("materiality_assessment")
    )
    assert model_span.context is not None
    assert result.call.span_id == f"{model_span.context.span_id:016x}"
    metrics = telemetry.metrics.render().decode()
    assert (
        'pypi_reasoning_model_calls_total{purpose="materiality_assessment",outcome="completed",service_tier="flex"} 1'
        in metrics
    )
    assert (
        'pypi_reasoning_model_tokens_total{purpose="materiality_assessment",type="cached_input"} 20'
        in metrics
    )
    assert (
        'pypi_reasoning_model_tokens_total{purpose="materiality_assessment",type="cache_write"} 30'
        in metrics
    )
    assert model_span.attributes is not None
    assert model_span.attributes["pypi.gen_ai.cache_write_tokens"] == 30
