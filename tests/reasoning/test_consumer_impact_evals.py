from __future__ import annotations

import json
from pathlib import Path

import pytest

from reasoning_worker.validation import impact_case_errors
from scripts.run_consumer_impact_evals import evaluate_consumer_impact

CASES_PATH = Path(__file__).parents[2] / "evals" / "consumer-impact" / "cases.json"


@pytest.mark.parametrize(
    "case", json.loads(CASES_PATH.read_text())["cases"], ids=lambda case: case["id"]
)
def test_consumer_impact_eval_corpus(case):
    result = evaluate_consumer_impact(case["applicability"], case["release_context"])
    assert result.passed is case["expected_pass"]


def test_update_and_review_are_valid_consumer_actions():
    assert not impact_case_errors(
        "A consumer updates dependencies that reach urllib3 2.7.0 on Python 3.9. "
        "Because urllib3 2.7.0 declares Requires-Python >=3.10, the installer rejects it."
    )
    assert not impact_case_errors(
        "A consumer reviews urllib3 2.7.0 support for Python 3.9. Because urllib3 2.7.0 "
        "declares Requires-Python >=3.10, compatibility tools report Python 3.9 as excluded."
    )
    assert not impact_case_errors(
        "A consumer generates dependencies for requests 2.32.5 targeting Python 3.8. "
        "Since requests 2.32.5 declares Requires-Python >=3.9, dependency tooling cannot "
        "select that release."
    )
    assert not impact_case_errors(
        "A consumer queries classifier-based compatibility for requests 2.32.5 because "
        "requests 2.32.5 removes Python 3.8 and adds Python 3.14 classifiers, causing the "
        "reported support list to change."
    )


def test_installing_is_a_valid_consumer_action():
    assert not impact_case_errors(
        "Installing requests 2.33.0 on Python 3.9 may fail because its Requires-Python rule "
        "changed from >=3.9 to >=3.10, causing enforcing installers to reject the package."
    )


def test_impact_case_accepts_natural_causal_order_and_dependency_range_language():
    assert not impact_case_errors(
        "When a consumer installs requests 2.33.0 on Python 3.9, the installer rejects it "
        "because the minimum supported Python version increased from Python 3.9 to Python 3.10."
    )
    assert not impact_case_errors(
        "A consumer installs boto3 1.40.0 because the required botocore range changed from "
        ">=1.39.17,<1.40.0 to >=1.40.0,<1.41.0, which excludes botocore 1.39.x."
    )


def test_runtime_impact_case_requires_a_concrete_trigger_and_behavior():
    statement = (
        "When an application uses urllib3 2.7.0 to follow a different-host redirect, it removes "
        "configured headers because Retry.remove_headers_on_redirect is non-empty."
    )

    assert not impact_case_errors(statement, kinds=("runtime",))
    assert impact_case_errors(statement, kinds=("metadata",))


def test_runtime_validator_accepts_the_observed_urllib3_applicability_wording():
    statement = (
        "When urllib3 2.7.0 follows a redirect to a different host with "
        "Retry.remove_headers_on_redirect non-empty, listed headers are stripped because "
        "cross-host handling changed from retaining them to removing them."
    )

    assert not impact_case_errors(statement, kinds=("runtime",))
