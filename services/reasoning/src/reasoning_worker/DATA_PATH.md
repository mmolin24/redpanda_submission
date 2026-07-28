# Reasoning Worker Data Path

The worker turns one monitored PyPI release into either a customer-visible
finding or a recorded processing failure.

```text
PyPI RSS -> Redpanda Connect
              | invalid -> pypi.ingest-failures.v1 -> sink -> PostgreSQL
              | unmonitored -> ignored
              | monitored -> pypi.releases.v1 -> reasoning worker
                                                    -> pypi.findings.v1 or pypi.failures.v1
                                                    -> sink -> PostgreSQL -> API -> UI
```

## One record, step by step

| Step | What happens | Primary code |
| ---: | --- | --- |
| 1 | Redpanda Connect parses RSS, rejects invalid input, filters unmonitored packages, and publishes a normalized release. | [`source-resources.yaml`](../../../../config/connect/source-resources.yaml#L108-L340) |
| 2 | The worker consumes the record and validates the `ReleaseEvent` contract. | [`runtime.py`](runtime.py#L196-L245), [`models.py`](models.py#L225-L360) |
| 3 | PyPI metadata and optional artifacts are collected and normalized into an `EvidenceBundle`. | [`evidence.py`](evidence.py#L145-L230), [`evidence.py`](evidence.py#L458-L560) |
| 4 | The workflow chooses the cheapest supported route. | [`workflow.py`](workflow.py#L584-L635) |
| 5 | Deterministic rules produce a conclusion when possible; otherwise three bounded model-assisted stages run. | [`deterministic_impact.py`](deterministic_impact.py#L59-L226), [`reasoning.py`](reasoning.py#L167-L607) |
| 6 | The conclusion is validated and converted into a typed `Finding` or `FailureRecord`. | [`workflow.py`](workflow.py#L444-L531), [`validation.py`](validation.py#L158-L344) |
| 7 | The terminal record is bounded, published, and acknowledged by Redpanda. Only then is the input offset committed. | [`terminal.py`](terminal.py#L246-L283), [`runtime.py`](runtime.py#L285-L386) |
| 8 | The sink validates the terminal schema and writes the result to PostgreSQL for the API and UI. | [`sink.yaml`](../../../../config/connect/sink.yaml#L4-L105) |

## Routing decision

| Route | When it is used | Model calls | Output |
| --- | --- | ---: | --- |
| `observe_only` | The release is a prerelease. | 0 | Non-actionable finding |
| `deterministic_non_substantive` | Complete evidence proves nothing relevant changed. | 0 | Non-substantive finding |
| `deterministic_impact` | Metadata or artifacts prove a consumer impact. | 0 | Publishable finding |
| `model` | The evidence requires semantic interpretation. | Bounded | Model-assisted finding or failure |

The model-assisted path is intentionally small:

1. [`MaterialityEngine`](reasoning.py#L167-L332) decides whether the change matters.
2. [`ApplicabilityEngine`](reasoning.py#L341-L455) explains who could be affected and under what condition.
3. [`CustomerImpactEngine`](reasoning.py#L464-L607) creates the concise customer-facing result.

Both paths use the same normalized evidence and publication contract. The
recorded `analysis_method` is either `deterministic` or `model_assisted`.

## Delivery rule

```text
terminal publish acknowledged -> input offset committed
```

This ordering is the reliability boundary. The worker must never commit the
input first.

| Outcome | Terminal topic | Input offset |
| --- | --- | --- |
| Supported conclusion, including insufficient evidence | `pypi.findings.v1` | Commit after acknowledgement |
| Invalid worker input or permanent processing failure | `pypi.failures.v1` | Commit after acknowledgement |
| Retryable provider, enrichment, or publication failure | Nothing published yet | Do not commit; retry |
| Monitored-package configuration mismatch | Nothing published | Do not commit; apply backpressure |
