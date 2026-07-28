# Model Cost Optimization

## Where we started

The first design question was whether one large model call would be cheaper and
simpler than several focused calls. It was not. In a development comparison,
the combined call cost `$0.127680`, compared with `$0.062433` for the three-stage
path, and produced less focused customer copy.

That result changed the optimization target. The goal is not merely to reduce
the number of calls. It is to avoid reasoning when structured evidence already
supports a conclusion, then keep the remaining reasoning narrow and testable.

## Where we landed

Every record now passes through lower-cost deterministic work first:

1. Redpanda Connect removes valid unmonitored releases.
2. The worker normalizes candidate and prior-release evidence and computes the
   change set.
3. Package-neutral rules complete prerelease, unchanged-release, Python support,
   wheel/platform, source-distribution, and yank cases with zero model calls.
4. Dependency, vulnerability, partial, or ambiguous changes enter three bounded
   stages: materiality, applicability, and customer-impact summary.
5. Corrections are bounded, and paid mode requires explicit configuration.

The latest clean paid canary, `boto3 1.39.1`, completed the standard three
stages without correction calls for an estimated `$0.032143`.

## Current measured baseline

As of July 28, 2026, the retained local sample contains 7 paid package analyses
and 24 billable model calls:

| Measure | Observed value |
| --- | ---: |
| Total estimated cost | `$0.505347` |
| Mean per model-assisted analysis | `$0.072192` |
| Median per model-assisted analysis | `$0.060128` |
| Mean per individual model call | `$0.021056` |
| Per-analysis range | `$0.032143–$0.116483` |

This is a small development sample that includes repeated experiments, not a
production forecast or provider-invoice reconciliation. The useful planning
formula is:

```text
daily model cost = monitored releases × model-route rate × cost per model-assisted analysis
```

The strongest cost lever is therefore the model-route rate: keep customer scope
explicit, expand deterministic conclusions only where evidence is conclusive,
and use model reasoning for semantic interpretation rather than basic compute.
Tokens, latency, corrections, and estimated cost remain visible per stage so the
next optimization can be measured instead of assumed.
