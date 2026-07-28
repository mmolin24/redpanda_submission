from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from helpers import evidence, substantive
from test_provider import Client

from reasoning_worker.app import build_pipeline
from reasoning_worker.model_input import (
    ModelInputTooLarge,
    bound_collection,
    compile_model_input,
    encoded_json_bytes,
)
from reasoning_worker.models import (
    EvidenceFact,
    TokenUsage,
)
from reasoning_worker.monitoring import MonitoredPackages
from reasoning_worker.provider import (
    MATERIALITY_SCHEMA,
    FakeModelProvider,
    FakeOutcome,
    ModelRequest,
    OpenAIResponsesProvider,
    capture_model_payloads_from_environment,
)
from reasoning_worker.reasoning import MaterialityEngine
from reasoning_worker.telemetry import WorkerTelemetry
from reasoning_worker.workflow import route

_REASONING_INPUT_BUDGET_BYTES = 48 * 1024
_PROVIDER_USER_JSON_HARD_LIMIT_BYTES = 64 * 1024


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(value: object) -> str:
    encoded = _canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _assert_complete_omission_summary(
    *,
    included: list[dict[str, object]],
    original: list[dict[str, object]],
    summary: dict[str, object],
) -> None:
    original_bytes = _canonical_json(original).encode("utf-8")
    assert summary == {
        "omitted": True,
        "total_count": len(original),
        "included_count": len(included),
        "omitted_count": len(original) - len(included),
        "full_content_bytes": len(original_bytes),
        "full_content_sha256": _sha256(original),
    }
    assert 0 < len(included) < len(original)
    assert all(item in original for item in included)


def _adversarial_bundle():
    bundle = evidence()
    facts = tuple(
        EvidenceFact(
            evidence_id=f"context.documents.{index}.text",
            value={
                "detail": (f"fact-{index:03d}-λ🚀" * 90),
                "authorization": f"Bearer should-not-cross-{index}",
            },
            source="context",
        )
        for index in range(120)
    )
    return replace(bundle, facts=facts)


def _routing_for(bundle):
    return route(is_prerelease=False, evidence=bundle)


def test_materiality_compiles_multibyte_facts_with_a_complete_omission_manifest():
    bundle = _adversarial_bundle()
    first = FakeModelProvider(
        [
            FakeOutcome(
                parsed=substantive(
                    evidence_id="context.documents.0.text",
                )
            )
        ]
    )
    second = FakeModelProvider(
        [
            FakeOutcome(
                parsed=substantive(
                    evidence_id="context.documents.0.text",
                )
            )
        ]
    )

    MaterialityEngine(first).evaluate(bundle, _routing_for(bundle))
    MaterialityEngine(second).evaluate(bundle, _routing_for(bundle))

    first_input = first.requests[0].model_input
    second_input = second.requests[0].model_input
    assert first_input == second_input
    assert len(_canonical_json(first_input).encode("utf-8")) <= (_REASONING_INPUT_BUDGET_BYTES)
    assert "should-not-cross" not in _canonical_json(first_input)

    included = first_input["evidence"]["facts"]
    sanitized_original = [
        {
            "evidence_id": fact.evidence_id,
            "value": {
                "detail": fact.value["detail"],
                "authorization": "[REDACTED:secret-field]",
            },
            "source": fact.source,
        }
        for fact in bundle.facts
    ]
    _assert_complete_omission_summary(
        included=included,
        original=sanitized_original,
        summary=first_input["evidence"]["facts_summary"],
    )


def test_materiality_replaces_one_oversized_fact_with_an_explicit_value_manifest():
    bundle = evidence()
    oversized_value = {"text": "λ🚀" * 10_000}
    oversized_fact = EvidenceFact(
        evidence_id="context.documents.0.text",
        value=oversized_value,
        source="context",
    )
    bundle = replace(bundle, facts=(oversized_fact,))

    model_view = bundle.model_view()

    assert model_view["facts"] == [
        {
            "evidence_id": oversized_fact.evidence_id,
            "source": oversized_fact.source,
            "value": {
                "omitted": True,
                "reason": "value_exceeds_model_budget",
                "full_content_bytes": len(_canonical_json(oversized_value).encode("utf-8")),
                "full_content_sha256": _sha256(oversized_value),
            },
        }
    ]
    assert model_view["facts_summary"]["summarized_count"] == 1
    assert model_view["facts_summary"]["omitted"] is True
    assert model_view["facts_summary"]["full_content_sha256"] == _sha256(
        [
            {
                "evidence_id": oversized_fact.evidence_id,
                "source": oversized_fact.source,
                "value": oversized_value,
            }
        ]
    )


def _request(model_input):
    return ModelRequest(
        purpose="materiality_assessment",
        instructions="Classify",
        model_input=model_input,
        output_schema=MATERIALITY_SCHEMA,
        output_schema_name="materiality_assessment",
    )


def test_openai_user_message_is_complete_parseable_canonical_json(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_ENV", "local")
    monkeypatch.setenv("OBS_CAPTURE_MODEL_PAYLOADS", "true")
    client = Client()
    client.calls = 1
    model_input = {
        "evidence": {
            "facts": [
                {
                    "evidence_id": "context.multibyte",
                    "value": "λ🚀" * 2_000,
                    "source": "context",
                }
            ]
        }
    }

    OpenAIResponsesProvider(client).complete(_request(model_input))

    user_message = client.bodies[-1]["input"][1]["content"]
    assert user_message == _canonical_json(model_input)
    assert json.loads(user_message) == model_input
    assert len(user_message.encode("utf-8")) <= (_PROVIDER_USER_JSON_HARD_LIMIT_BYTES)


def test_provider_rejects_over_budget_user_json_before_any_client_call(
    monkeypatch,
):
    monkeypatch.setenv("DEPLOYMENT_ENV", "local")
    monkeypatch.setenv("OBS_CAPTURE_MODEL_PAYLOADS", "true")
    client = Client()
    client.calls = 1
    provider = OpenAIResponsesProvider(client)
    model_input = {"evidence": {"facts": [{"value": "x" * 80_000}]}}

    with pytest.raises(
        RuntimeError,
        match=r"(model input|provider|user JSON).*(budget|limit)",
    ):
        provider.complete(_request(model_input))

    assert client.bodies == []
    assert client.headers == []


def test_model_input_compiler_never_silently_drops_list_items():
    model_input = {
        "evidence": {
            "facts": [{"evidence_id": f"fact-{index:03d}", "value": index} for index in range(501)]
        }
    }

    compiled = compile_model_input(model_input)

    assert compiled.value == model_input
    assert len(compiled.value["evidence"]["facts"]) == 501
    assert json.loads(compiled.canonical_json) == model_input


@pytest.mark.parametrize(
    "values",
    [
        list(range(501)),
        [{"index": index, "value": "x" * 1_000} for index in range(501)],
    ],
    ids=["count-cap", "byte-cap"],
)
def test_bounded_collections_enforce_explicit_count_and_byte_caps(values):
    selection = bound_collection(values)

    assert len(selection.items) <= 500
    assert encoded_json_bytes(selection.items) <= 32 * 1024
    assert selection.summary["total_count"] == len(values)
    assert selection.summary["included_count"] == len(selection.items)
    assert selection.summary["omitted_count"] == (len(values) - len(selection.items))


def test_reasoning_request_enforces_48_kib_aggregate_budget_before_fake_consumption():
    bundle = _adversarial_bundle()
    bundle = replace(
        bundle,
        computed={
            **bundle.computed,
            "missing": ["aggregate-budget-padding-" + ("m" * 20_000)],
        },
    )
    rendered_bytes = len(_canonical_json({"evidence": bundle.model_view()}).encode("utf-8"))
    assert 48 * 1024 < rendered_bytes < 64 * 1024
    provider = FakeModelProvider(
        [
            FakeOutcome(
                parsed=substantive(
                    evidence_id="context.documents.0.text",
                )
            )
        ]
    )

    with pytest.raises(ModelInputTooLarge):
        MaterialityEngine(provider).evaluate(bundle, _routing_for(bundle))

    assert provider.requests == []
    assert len(provider.outcomes) == 1


def test_fake_overbudget_rejection_is_atomic_and_preserves_queued_outcome():
    queued = FakeOutcome(parsed={"decision": "non_substantive"})
    provider = FakeModelProvider([queued])

    with pytest.raises(ModelInputTooLarge):
        provider.complete(_request({"oversized": "x" * (65 * 1024)}))

    assert provider.requests == []
    assert list(provider.outcomes) == [queued]

    result = provider.complete(_request({"evidence": {"facts": []}}))

    assert result.parsed == queued.parsed
    assert len(provider.requests) == 1
    assert list(provider.outcomes) == []


def test_fake_and_openai_share_the_exact_compiled_user_json(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_ENV", "local")
    monkeypatch.setenv("OBS_CAPTURE_MODEL_PAYLOADS", "true")
    model_input = {
        "release": "dependency-b 2.0.0",
        "evidence": {
            "facts": [
                {
                    "evidence_id": "context.multibyte",
                    "value": "λ🚀" * 2_000,
                    "source": "context",
                }
            ]
        },
    }
    request = _request(model_input)
    fake = FakeModelProvider([FakeOutcome(parsed={"decision": "non_substantive"})]).complete(
        request
    )
    client = Client()
    client.calls = 1
    OpenAIResponsesProvider(client).complete(request)

    assert fake.call.request_payload is not None
    fake_input = fake.call.request_payload["input"]
    openai_user_json = client.bodies[-1]["input"][1]["content"]
    assert openai_user_json == _canonical_json(fake_input)
    assert json.loads(openai_user_json) == fake_input


def test_response_hash_covers_the_full_visible_response_without_slicing(
    monkeypatch,
):
    monkeypatch.setenv("DEPLOYMENT_ENV", "local")
    monkeypatch.setenv("OBS_CAPTURE_MODEL_PAYLOADS", "true")
    parsed = {
        "items": [{"index": index, "value": f"visible-{index:03d}"} for index in range(501)],
        "narrative": ("benign-visible-content-" * 700) + "visible-tail-marker",
    }
    client = Client()
    client.responses.create = lambda **_body: SimpleNamespace(
        id="resp_full_visible",
        status="completed",
        model="gpt-5.6-sol-2026-07-01",
        service_tier="default",
        output=[],
        output_text=json.dumps(parsed),
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        incomplete_details=None,
        _request_id="req_full_visible",
    )

    call = OpenAIResponsesProvider(client).complete(_request({"evidence": {"facts": []}})).call

    assert call.response_payload is not None
    assert call.response_payload["parsed"]["items"][-1] == parsed["items"][-1]
    assert call.response_payload["parsed"]["narrative"].endswith("visible-tail-marker")
    assert call.response_sha256 == _sha256(call.response_payload)


def test_fake_response_hash_covers_the_full_redacted_visible_response(
    monkeypatch,
):
    monkeypatch.setenv("DEPLOYMENT_ENV", "local")
    monkeypatch.setenv("OBS_CAPTURE_MODEL_PAYLOADS", "true")
    parsed = {
        "items": list(range(501)),
        "narrative": ("benign-visible-content-" * 700) + "visible-tail-marker",
    }

    call = (
        FakeModelProvider([FakeOutcome(parsed=parsed)])
        .complete(_request({"evidence": {"facts": []}}))
        .call
    )

    assert call.response_payload is not None
    assert call.response_payload["parsed"]["items"][-1] == 500
    assert call.response_payload["parsed"]["narrative"].endswith("visible-tail-marker")
    assert call.response_sha256 == _sha256(call.response_payload)


def _run_fake_call() -> Any:
    return (
        FakeModelProvider(
            [
                FakeOutcome(
                    parsed={"decision": "non_substantive"},
                    usage=TokenUsage(input_tokens=7, output_tokens=3),
                )
            ]
        )
        .complete(_request({"evidence": {"facts": []}}))
        .call
    )


def _run_openai_call() -> Any:
    client = Client()
    client.calls = 1
    return OpenAIResponsesProvider(client).complete(_request({"evidence": {"facts": []}})).call


@pytest.mark.parametrize("adapter", ["fake", "openai"])
def test_model_payload_capture_requires_explicit_local_opt_in(
    monkeypatch,
    adapter,
):
    matrix = [
        ("local", "true", True),
        ("local", "false", False),
        ("local", None, False),
        ("production", "true", False),
        ("production", "false", False),
        ("production", None, False),
        (None, "true", False),
        (None, "false", False),
        (None, None, False),
    ]
    expected_hashes = None
    for deployment, capture_flag, retained in matrix:
        if deployment is None:
            monkeypatch.delenv("DEPLOYMENT_ENV", raising=False)
        else:
            monkeypatch.setenv("DEPLOYMENT_ENV", deployment)
        if capture_flag is None:
            monkeypatch.delenv("OBS_CAPTURE_MODEL_PAYLOADS", raising=False)
        else:
            monkeypatch.setenv(
                "OBS_CAPTURE_MODEL_PAYLOADS",
                capture_flag,
            )

        call = _run_fake_call() if adapter == "fake" else _run_openai_call()
        case = (adapter, deployment, capture_flag)
        assert (call.request_payload is not None) is retained, case
        assert (call.response_payload is not None) is retained, case
        assert call.request_sha256.startswith("sha256:"), case
        assert call.response_sha256.startswith("sha256:"), case
        if expected_hashes is None:
            expected_hashes = (
                call.request_sha256,
                call.response_sha256,
            )
        assert (
            call.request_sha256,
            call.response_sha256,
        ) == expected_hashes, case
        assert call.usage.input_tokens > 0, case
        assert call.attempts, case
        assert call.attempts[0].client_request_id, case
        assert call.attempts[0].openai_request_id, case
        assert call.model_call_id, case
        assert call.purpose == "materiality_assessment", case


def test_capture_policy_rejects_an_invalid_flag_before_pipeline_consumption(
    monkeypatch,
):
    monkeypatch.setenv("MODEL_MODE", "fake")
    monkeypatch.setenv(
        "FIXTURE_HISTORY_PATH",
        "data/fixtures/package-history.json",
    )
    monkeypatch.setenv("DEPLOYMENT_ENV", "local")
    monkeypatch.setenv("OBS_CAPTURE_MODEL_PAYLOADS", "sometimes")
    with pytest.raises(ValueError, match="OBS_CAPTURE_MODEL_PAYLOADS"):
        build_pipeline(MonitoredPackages(("urllib3",)), WorkerTelemetry())


@pytest.mark.parametrize(
    "raw_flag",
    ["TRUE", " false"],
)
def test_capture_flag_rejects_case_and_whitespace_variants(
    monkeypatch,
    raw_flag,
):
    monkeypatch.setenv("DEPLOYMENT_ENV", "local")
    monkeypatch.setenv("OBS_CAPTURE_MODEL_PAYLOADS", raw_flag)

    with pytest.raises(ValueError, match="OBS_CAPTURE_MODEL_PAYLOADS"):
        capture_model_payloads_from_environment()


@pytest.mark.parametrize("adapter", ["fake", "openai"])
def test_direct_capture_override_cannot_retain_payloads_in_production(
    monkeypatch,
    adapter,
):
    monkeypatch.setenv("DEPLOYMENT_ENV", "production")
    monkeypatch.setenv("OBS_CAPTURE_MODEL_PAYLOADS", "true")

    constructor: Callable[..., object]
    arguments: tuple[object, ...]
    if adapter == "fake":
        constructor = FakeModelProvider
        arguments = ([FakeOutcome(parsed={"decision": "non_substantive"})],)
    else:
        constructor = OpenAIResponsesProvider
        arguments = (Client(),)

    with pytest.raises(TypeError, match="capture_payloads"):
        constructor.__call__(*arguments, capture_payloads=True)


@pytest.mark.parametrize("adapter", ["fake", "openai"])
def test_capture_policy_cannot_be_escalated_after_provider_construction(
    monkeypatch,
    adapter,
):
    monkeypatch.setenv("DEPLOYMENT_ENV", "production")
    monkeypatch.setenv("OBS_CAPTURE_MODEL_PAYLOADS", "true")
    if adapter == "fake":
        provider = FakeModelProvider([FakeOutcome(parsed={"decision": "non_substantive"})])
    else:
        client = Client()
        client.calls = 1
        provider = OpenAIResponsesProvider(client)

    assert provider.capture_payloads is False
    with pytest.raises(AttributeError):
        object.__setattr__(provider, "capture_payloads", True)

    call = provider.complete(_request({"evidence": {"facts": []}})).call
    assert call.request_payload is None
    assert call.response_payload is None
