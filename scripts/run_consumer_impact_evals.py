#!/usr/bin/env python3
"""Run the offline customer-impact readability evaluation corpus."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "reasoning" / "src"))

from reasoning_worker.models import Json  # noqa: E402
from reasoning_worker.validation import (  # noqa: E402
    impact_case_errors,
    impact_case_kinds,
)

_INTERNAL_LANGUAGE = re.compile(
    r"\b(deterministic|internal ranking|gate [234]|resolver|context ids?)\b",
    re.IGNORECASE,
)
_CONSEQUENCE_LANGUAGE = re.compile(
    r"\b(behavior|build|compatib|incompatib|deploy|error|fail|install|lockfile|"
    r"request|response|redirect|header|remov|strip|warning|exception|resolv|resolution|"
    r"runtime|security|traffic|withdraw|worker)\w*\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EvalCriterion:
    """Record one customer-impact readability assertion."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class ConsumerImpactEval:
    """Aggregate customer-impact readability criteria for one finding."""

    passed: bool
    criteria: tuple[EvalCriterion, ...]

    def to_dict(self) -> Json:
        return asdict(self)


def evaluate_consumer_impact(applicability: Json, release_context: Json) -> ConsumerImpactEval:
    """Evaluate whether applicability output is concise, grounded, and actionable."""
    raw_scenarios = applicability.get("consumer_scenarios", [])
    scenarios = (
        [item for item in raw_scenarios if isinstance(item, dict)]
        if isinstance(raw_scenarios, list)
        else []
    )
    package = str(release_context.get("package", "")).strip()
    version = str(release_context.get("candidate_version", "")).strip()
    raw_change_types = release_context.get("change_types", [])
    change_types = (
        [str(item) for item in raw_change_types] if isinstance(raw_change_types, list) else []
    )
    case_kinds = impact_case_kinds(change_types) if change_types else ("metadata",)
    criteria = [
        EvalCriterion(
            "has_consumer_scenario",
            bool(scenarios),
            "A publishable generic finding needs at least one concrete consumer scenario.",
        )
    ]

    raw_conditions = applicability.get("impact_conditions", [])
    general_conditions = (
        [str(item) for item in raw_conditions] if isinstance(raw_conditions, list) else []
    )
    for index, condition in enumerate(general_conditions):
        case_errors = impact_case_errors(condition, kinds=case_kinds)
        criteria.append(
            EvalCriterion(
                f"condition_{index + 1}_names_release",
                bool(package and version)
                and package.casefold() in condition.casefold()
                and version.casefold() in condition.casefold(),
                f"Every scenario must name {package or 'the package'} and {version or 'the version'}.",
            )
        )
        criteria.append(
            EvalCriterion(
                f"condition_{index + 1}_builds_causal_case",
                not case_errors,
                "; ".join(case_errors)
                if case_errors
                else "Condition states the consumer action, exact changed behavior, and outcome.",
            )
        )

    for index, scenario in enumerate(scenarios):
        prefix = f"scenario_{index + 1}"
        headline = str(scenario.get("headline", "")).strip()
        statement = str(scenario.get("statement", "")).strip()
        verification = str(scenario.get("verification", "")).strip()
        conditions = scenario.get("conditions", [])
        condition_text = (
            " ".join(str(item) for item in conditions) if isinstance(conditions, list) else ""
        )
        statement_errors = impact_case_errors(statement, kinds=case_kinds)
        visible_text = " ".join((headline, statement, verification, condition_text))
        criteria.extend(
            (
                EvalCriterion(
                    f"{prefix}_explains_consequence",
                    bool(_CONSEQUENCE_LANGUAGE.search(statement)),
                    "Scenario must name a concrete package-consumer consequence.",
                ),
                EvalCriterion(
                    f"{prefix}_builds_causal_case",
                    bool(condition_text)
                    and package.casefold() in statement.casefold()
                    and version.casefold() in statement.casefold()
                    and not statement_errors,
                    "Scenario statement must connect the named release, consumer action, exact "
                    "changed behavior, and outcome; conditions must list the atomic predicates.",
                ),
                EvalCriterion(
                    f"{prefix}_has_verification",
                    bool(verification),
                    "Scenario must give one concrete verification step.",
                ),
                EvalCriterion(
                    f"{prefix}_is_auditable",
                    bool(scenario.get("evidence_ids")),
                    "Scenario must cite accepted release evidence.",
                ),
                EvalCriterion(
                    f"{prefix}_uses_plain_language",
                    not bool(_INTERNAL_LANGUAGE.search(visible_text)),
                    "Consumer copy must not expose pipeline vocabulary.",
                ),
                EvalCriterion(
                    f"{prefix}_is_scannable",
                    _word_count(headline) <= 10
                    and _word_count(statement) <= 35
                    and _word_count(verification) <= 25,
                    "Headline, statement, and verification must stay within 10, 35, and 25 words.",
                ),
            )
        )
    return ConsumerImpactEval(all(item.passed for item in criteria), tuple(criteria))


def _word_count(value: str) -> int:
    return len(re.findall(r"\b[\w.-]+\b", value))


def main() -> int:
    """Evaluate every checked-in consumer-impact case and report failures."""
    parser = argparse.ArgumentParser(
        description="Grade generic applicability assessment consumer-impact output."
    )
    parser.add_argument("--input", type=Path, help="Grade one finding-detail JSON file.")
    args = parser.parse_args()
    if args.input:
        payload = json.loads(args.input.read_text())
        applicability = payload.get("gate_results", {}).get(
            "applicability", payload.get("applicability", {})
        )
        release_context = payload.get("release_context") or {
            "package": payload.get("package_name"),
            "candidate_version": payload.get("candidate_version"),
        }
        result = evaluate_consumer_impact(applicability, release_context)
        print(f"{'PASS' if result.passed else 'FAIL'} {args.input}")
        for criterion in result.criteria:
            print(
                f"  {'PASS' if criterion.passed else 'FAIL'} {criterion.name}: {criterion.detail}"
            )
        return 0 if result.passed else 1

    corpus = json.loads((ROOT / "evals" / "consumer-impact" / "cases.json").read_text())
    failures = 0
    for case in corpus["cases"]:
        result = evaluate_consumer_impact(case["applicability"], case["release_context"])
        matched = result.passed is case["expected_pass"]
        print(
            f"{'PASS' if matched else 'FAIL'} {case['id']} expected={case['expected_pass']} actual={result.passed}"
        )
        failures += not matched
    print(
        f"{len(corpus['cases']) - failures}/{len(corpus['cases'])} eval cases matched expectations"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
