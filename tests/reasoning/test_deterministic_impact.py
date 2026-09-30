from __future__ import annotations

from helpers import event

from reasoning_worker.deterministic_impact import compile_deterministic_impact
from reasoning_worker.evidence import MetadataEvidenceBuilder
from reasoning_worker.models import Disposition, Finding
from reasoning_worker.provider import FakeModelProvider
from reasoning_worker.reasoning import ApplicabilityEngine, MaterialityEngine
from reasoning_worker.workflow import ReasoningPipeline, StaticEnricher, route


def _file(filename: str, package_type: str = "bdist_wheel") -> dict[str, object]:
    return {
        "filename": filename,
        "packagetype": package_type,
        "digests": {"sha256": filename},
    }


def _native_bundle(package: str = "native-bridge"):
    release_event = event(package=package)
    distribution = package.replace("-", "_")
    baseline = {
        "info": {
            "name": package,
            "version": "1.9.0",
            "requires_python": ">=3.9",
            "requires_dist": [],
        },
        "urls": [
            _file(f"{distribution}-1.9.0-cp39-cp39-macosx_10_13_x86_64.whl"),
            _file(f"{distribution}-1.9.0-cp310-cp310-macosx_10_13_x86_64.whl"),
            _file(f"{distribution}-1.9.0.tar.gz", "sdist"),
        ],
        "vulnerabilities": [],
    }
    candidate = {
        "info": {
            "name": package,
            "version": release_event.release.version,
            "requires_python": ">=3.10",
            "requires_dist": [],
        },
        "urls": [
            _file(f"{distribution}-2.0.0-cp310-cp310-macosx_10_15_x86_64.whl"),
            _file(f"{distribution}-2.0.0-cp315-cp315-ios_13_0_arm64_iphoneos.whl"),
            _file(f"{distribution}-2.0.0.tar.gz", "sdist"),
        ],
        "vulnerabilities": [],
    }
    bundle = MetadataEvidenceBuilder().build(
        release_event,
        baseline,
        candidate,
        context={
            "code_hunks": [
                {
                    "evidence_id": "artifact.hunk.sha256:123",
                    "path": "src/native.c",
                    "text": "bounded source change",
                }
            ]
        },
    )
    return release_event, bundle


def test_compiler_is_package_neutral_and_resolves_python_platform_and_support_facts():
    _, bundle = _native_bundle("native-bridge")

    compiled = compile_deterministic_impact(bundle)

    assert compiled is not None
    proof = compiled.gate_results["deterministic_impact"]
    assert proof["decision"] == "impact_detected"
    assert {impact["dimension"] for impact in proof["impacts"]} == {
        "python_version",
        "platform",
    }
    assert proof["impacts"][0]["affected_if"].endswith("on Python 3.9.")
    assert "before 10.15" in proof["impacts"][1]["affected_if"]
    support_values = {
        value for expansion in proof["support_expansions"] for value in expansion["values"]
    }
    assert support_values == {"Python 3.15 wheels", "iOS ARM64 wheels"}
    assert proof["limitations"] == [
        "This deterministic conclusion covers PyPI release metadata and published distribution artifacts.",
        "Source-level runtime behavior and undocumented changes were not evaluated by these rules.",
    ]
    assert "cffi" not in repr(compiled.gate_results).lower()


def test_macos_target_change_merges_interpreters_and_abis_and_preserves_other_coverage():
    release_event = event(package="native-bridge")
    distribution = "native_bridge"
    baseline = {
        "info": {
            "name": "native-bridge",
            "version": "1.9.0",
            "requires_python": ">=3.9",
            "requires_dist": [],
        },
        "urls": [
            _file(f"{distribution}-1.9.0-cp310-cp310-macosx_10_13_x86_64.whl"),
            _file(f"{distribution}-1.9.0-cp310-abi3-macosx_10_13_x86_64.whl"),
            _file(f"{distribution}-1.9.0-cp311-cp311-macosx_10_13_x86_64.whl"),
            _file(f"{distribution}-1.9.0-cp310-cp310-manylinux_2_17_x86_64.whl"),
        ],
        "vulnerabilities": [],
    }
    candidate = {
        "info": {
            "name": "native-bridge",
            "version": "2.0.0",
            "requires_python": ">=3.9",
            "requires_dist": [],
        },
        "urls": [
            _file(f"{distribution}-2.0.0-cp310-cp310-macosx_10_15_x86_64.whl"),
            _file(f"{distribution}-2.0.0-cp310-abi3-macosx_10_15_x86_64.whl"),
            _file(f"{distribution}-2.0.0-cp311-cp311-macosx_10_15_x86_64.whl"),
        ],
        "vulnerabilities": [],
    }
    bundle = MetadataEvidenceBuilder().build(release_event, baseline, candidate)

    compiled = compile_deterministic_impact(bundle)

    assert compiled is not None
    impacts = compiled.gate_results["deterministic_impact"]["impacts"]
    macos_impact, residual_coverage = impacts
    assert macos_impact["dimension"] == "platform"
    assert "before 10.15" in macos_impact["affected_if"]
    assert macos_impact["evidence_ids"] == [
        "baseline.files.1.filename",
        "baseline.files.0.filename",
        "baseline.files.2.filename",
        "candidate.files.1.filename",
        "candidate.files.0.filename",
        "candidate.files.2.filename",
    ]
    assert residual_coverage["dimension"] == "wheel_coverage"
    assert residual_coverage["evidence_ids"] == ["baseline.files.3.filename"]


def test_deterministic_impact_is_a_publishable_zero_model_terminal():
    release_event, bundle = _native_bundle("ffi-adapter")
    provider = FakeModelProvider([])
    worker = ReasoningPipeline(
        enricher=StaticEnricher(bundle),
        materiality=MaterialityEngine(provider),
        applicability=ApplicabilityEngine(provider),
    )

    terminal = worker.process(release_event)

    assert isinstance(terminal, Finding)
    assert terminal.disposition == Disposition.PUBLISHABLE
    assert terminal.publishable is True
    assert terminal.analysis_method == "deterministic"
    assert terminal.routing["analysis_eligibility"] == "deterministic_impact"
    assert terminal.analysis_metadata["model_calls"] == []
    assert terminal.gate_results["analysis_validation"]["path"] == {
        "valid": True,
        "errors": [],
    }


def test_yanked_release_is_resolved_for_an_unrelated_package():
    release_event = event(package="command-tool")
    baseline = {
        "info": {"name": "command-tool", "version": "1.9.0"},
        "urls": [_file("command_tool-1.9.0-py3-none-any.whl")],
        "vulnerabilities": [],
    }
    candidate = {
        "info": {"name": "command-tool", "version": "2.0.0"},
        "urls": [
            {
                **_file("command_tool-2.0.0-py3-none-any.whl"),
                "yanked": True,
                "yanked_reason": "incorrect release",
            }
        ],
        "vulnerabilities": [],
    }
    bundle = MetadataEvidenceBuilder().build(release_event, baseline, candidate)

    compiled = compile_deterministic_impact(bundle)

    assert compiled is not None
    proof = compiled.gate_results["deterministic_impact"]
    assert proof["impacts"][0]["dimension"] == "release_availability"
    assert "command-tool 2.0.0 is yanked" == proof["impacts"][0]["headline"]


def test_same_release_unyank_is_release_availability_not_platform_support():
    release_event = event(package="command-tool")
    observed_yanked = {
        "info": {"name": "command-tool", "version": "2.0.0"},
        "urls": [
            {
                **_file("command_tool-2.0.0-py3-none-any.whl"),
                "yanked": True,
                "yanked_reason": "incorrect release",
            }
        ],
        "vulnerabilities": [],
    }
    observed_unyanked = {
        "info": {"name": "command-tool", "version": "2.0.0"},
        "urls": [
            {
                **_file("command_tool-2.0.0-py3-none-any.whl"),
                "yanked": False,
            }
        ],
        "vulnerabilities": [],
    }
    bundle = MetadataEvidenceBuilder().build(
        release_event,
        observed_yanked,
        observed_unyanked,
    )

    compiled = compile_deterministic_impact(bundle)

    assert compiled is not None
    proof = compiled.gate_results["deterministic_impact"]
    summary = compiled.gate_results["customer_impact"]["customer_summary"]
    assert proof["decision"] == "support_expanded"
    assert proof["impacts"] == []
    assert proof["support_expansions"][0]["dimension"] == "release_availability"
    assert proof["support_expansions"][0]["change_type"] == "release_availability"
    assert summary["headline"] == "command-tool 2.0.0 is available again"
    assert summary["impact_type"] == "release_availability"
    assert "newly supported target" not in summary["affected_if"]
    assert summary["verification"] == ("Inspect the PyPI release status for command-tool 2.0.0.")


def test_support_only_change_is_a_deterministic_informational_finding():
    release_event = event(package="pure-client")
    baseline = {
        "info": {
            "name": "pure-client",
            "version": "1.9.0",
            "requires_python": ">=3.10",
        },
        "urls": [_file("pure_client-1.9.0-py3-none-any.whl")],
        "vulnerabilities": [],
    }
    candidate = {
        "info": {
            "name": "pure-client",
            "version": "2.0.0",
            "requires_python": ">=3.9",
        },
        "urls": [_file("pure_client-2.0.0-py3-none-any.whl")],
        "vulnerabilities": [],
    }
    bundle = MetadataEvidenceBuilder().build(release_event, baseline, candidate)

    compiled = compile_deterministic_impact(bundle)

    assert compiled is not None
    proof = compiled.gate_results["deterministic_impact"]
    assert proof["decision"] == "support_expanded"
    assert proof["impacts"] == []
    assert proof["support_expansions"][0]["values"] == ["Python 3.9"]
    provider = FakeModelProvider([])
    terminal = ReasoningPipeline(
        enricher=StaticEnricher(bundle),
        materiality=MaterialityEngine(provider),
        applicability=ApplicabilityEngine(provider),
    ).process(release_event)
    assert isinstance(terminal, Finding)
    assert terminal.gate_results["analysis_validation"]["path"]["valid"] is True
    assert provider.requests == []


def test_ambiguous_python_specifier_retains_model_assisted_routing():
    release_event = event(package="bounded-client")
    baseline = {
        "info": {
            "name": "bounded-client",
            "version": "1.9.0",
            "requires_python": ">=3.9,<4",
        },
        "urls": [_file("bounded_client-1.9.0-py3-none-any.whl")],
        "vulnerabilities": [],
    }
    candidate = {
        "info": {
            "name": "bounded-client",
            "version": "2.0.0",
            "requires_python": ">=3.10,<4",
        },
        "urls": [_file("bounded_client-2.0.0-py3-none-any.whl")],
        "vulnerabilities": [],
    }
    bundle = MetadataEvidenceBuilder().build(release_event, baseline, candidate)

    assert compile_deterministic_impact(bundle) is None
    assert route(is_prerelease=False, evidence=bundle).analysis_eligibility == "model"


def test_dependency_change_retains_model_assisted_routing():
    release_event = event(package="web-client")
    baseline = {
        "info": {
            "name": "web-client",
            "version": "1.9.0",
            "requires_dist": ["transport>=1"],
        },
        "urls": [_file("web_client-1.9.0-py3-none-any.whl")],
        "vulnerabilities": [],
    }
    candidate = {
        "info": {
            "name": "web-client",
            "version": "2.0.0",
            "requires_dist": ["transport>=2"],
        },
        "urls": [_file("web_client-2.0.0-py3-none-any.whl")],
        "vulnerabilities": [],
    }
    bundle = MetadataEvidenceBuilder().build(release_event, baseline, candidate)

    assert compile_deterministic_impact(bundle) is None
    assert route(is_prerelease=False, evidence=bundle).analysis_eligibility == "model"
