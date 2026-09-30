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
| 1 | Redpanda Connect parses RSS, rejects invalid input, filters unmonitored packages, and publishes a normalized release. | [`source-resources.yaml`](../../../../config/connect/source-resources.yaml#L104) |
| 2 | The worker consumes strict JSON, checks the schema version and required structure, and trusts Connect's field validation. | [`runtime.py`](runtime.py#L47), [`models.py`](models.py#L146) |
| 3 | PyPI metadata and optional artifacts are collected and normalized into an `EvidenceBundle`. | [`MetadataEvidenceBuilder.build`](evidence.py#L141), [`PyPIEnricher.enrich`](evidence.py#L467) |
| 4 | The workflow chooses the cheapest supported route. | [`route`](workflow.py#L578) |
| 5 | Deterministic rules produce a conclusion when possible; otherwise three bounded model-assisted stages run. | [`compile_deterministic_impact`](deterministic_impact.py#L68), [`MaterialityEngine`](reasoning.py#L167) |
| 6 | The conclusion is validated and converted into a typed `Finding` or `FailureRecord`. | [`_finding`](workflow.py#L447), [`_failure`](workflow.py#L537), [`validation.py`](validation.py#L158) |
| 7 | The terminal record is bounded, published, and acknowledged by Redpanda. Only then is the input offset committed. | [`prepare_terminal`](terminal.py#L244), [`runtime.py`](runtime.py#L347) |
| 8 | The sink validates the terminal schema and writes the result to PostgreSQL for the API and UI. | [`sink.yaml`](../../../../config/connect/sink.yaml#L32), [`sink output`](../../../../config/connect/sink.yaml#L90) |

## Routing decision

| Route | When it is used | Model calls | Output |
| --- | --- | ---: | --- |
| `observe_only` | The release is a prerelease. | 0 | Non-actionable finding |
| `deterministic_non_substantive` | Complete evidence proves nothing relevant changed. | 0 | Non-substantive finding |
| `deterministic_impact` | Metadata or artifacts prove a consumer impact. | 0 | Publishable finding |
| `model` | The evidence requires semantic interpretation. | Bounded | Model-assisted finding or failure |

The model-assisted path is intentionally small:

1. [`MaterialityEngine`](reasoning.py#L167) decides whether the change matters.
2. [`ApplicabilityEngine`](reasoning.py#L341) explains who could be affected and under what condition.
3. [`CustomerImpactEngine`](reasoning.py#L464) creates the concise customer-facing result.

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
