from __future__ import annotations

import io
import zipfile

import pytest
from helpers import event, substantive

from reasoning_worker.artifacts import (
    ArchiveInspector,
    ArtifactPolicy,
    UnsafeArtifact,
    diff_snapshots,
)
from reasoning_worker.evidence import MetadataEvidenceBuilder
from reasoning_worker.provider import FakeModelProvider, FakeOutcome
from reasoning_worker.reasoning import MaterialityEngine
from reasoning_worker.workflow import route


def archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for path, value in files.items():
            output.writestr(path, value)
    return buffer.getvalue()


def test_artifact_inspection_is_bounded_and_produces_manifest_and_hunks():
    inspector = ArchiveInspector()
    before = inspector.inspect(
        "pkg-1.whl", archive({"pkg/main.py": "VALUE = 1\n", "README.md": "old\n"})
    )
    after = inspector.inspect(
        "pkg-2.whl", archive({"pkg/main.py": "VALUE = 2\n", "README.md": "new\n"})
    )
    result = diff_snapshots(before, after)
    assert result["artifact_manifest_diff"]["changed"] == ["README.md", "pkg/main.py"]
    assert any("VALUE = 2" in hunk["diff"] for hunk in result["code_hunks"])
    assert result["documents"][0]["after"] == "new\n"


def test_versioned_sdist_roots_are_normalized_before_literal_diffing():
    inspector = ArchiveInspector()
    before = inspector.inspect(
        "urllib3-2.6.3.tar.gz",
        archive(
            {
                "urllib3-2.6.3/CHANGES.rst": "2.6.3\n======\nOld behavior.\n",
                "urllib3-2.6.3/PKG-INFO": "Name: urllib3\nVersion: 2.6.3\n",
                "urllib3-2.6.3/src/urllib3/response.py": "MODE = 'old'\n",
            }
        ),
    )
    after = inspector.inspect(
        "urllib3-2.7.0.tar.gz",
        archive(
            {
                "urllib3-2.7.0/CHANGES.rst": "2.7.0\n======\nNew behavior.\n",
                "urllib3-2.7.0/PKG-INFO": "Name: urllib3\nVersion: 2.7.0\n",
                "urllib3-2.7.0/src/urllib3/response.py": "MODE = 'new'\n",
            }
        ),
    )

    result = diff_snapshots(
        before,
        after,
        baseline_version="2.6.3",
        candidate_version="2.7.0",
    )

    assert result["artifact_comparison"]["baseline"]["stripped_root"] == "urllib3-2.6.3"
    assert result["artifact_comparison"]["candidate"]["stripped_root"] == "urllib3-2.7.0"
    assert result["artifact_manifest_diff"]["added"] == []
    assert result["artifact_manifest_diff"]["removed"] == []
    assert result["artifact_manifest_diff"]["changed"] == [
        "CHANGES.rst",
        "src/urllib3/response.py",
    ]
    assert result["artifact_manifest_diff"]["ignored_version_only"] == ["PKG-INFO"]
    assert [item["path"] for item in result["code_hunks"]] == ["src/urllib3/response.py"]
    source_hunk = next(
        item for item in result["code_hunks"] if item["path"] == "src/urllib3/response.py"
    )
    assert source_hunk["baseline_path"] == "urllib3-2.6.3/src/urllib3/response.py"
    assert source_hunk["candidate_path"] == "urllib3-2.7.0/src/urllib3/response.py"
    assert source_hunk["evidence_id"].startswith("artifact.text-diff.sha256:")
    assert "MODE = 'new'" in source_hunk["diff"]
    assert source_hunk["truncated"] is False
    assert result["documents"][0]["path"] == "CHANGES.rst"
    assert result["documents"][0]["evidence_id"].startswith("artifact.document-diff.sha256:")


def test_stable_artifact_records_become_directly_citable_evidence():
    inspector = ArchiveInspector()
    before = inspector.inspect(
        "pkg-1.0.0.tar.gz",
        archive({"pkg-1.0.0/src/pkg.py": "VALUE = 1\n"}),
    )
    after = inspector.inspect(
        "pkg-2.0.0.tar.gz",
        archive({"pkg-2.0.0/src/pkg.py": "VALUE = 2\n"}),
    )
    context = diff_snapshots(
        before,
        after,
        baseline_version="1.0.0",
        candidate_version="2.0.0",
    )
    bundle = MetadataEvidenceBuilder().build(
        event(package="pkg"),
        {
            "info": {"name": "pkg", "version": "1.0.0", "requires_python": ">=3.9"},
            "urls": [],
            "vulnerabilities": [],
        },
        {
            "info": {"name": "pkg", "version": "2.0.0", "requires_python": ">=3.9"},
            "urls": [],
            "vulnerabilities": [],
        },
        context=context,
    )

    hunk = context["code_hunks"][0]
    manifest_diff = context["artifact_manifest_diff"]
    assert hunk["evidence_id"] in bundle.evidence_ids
    assert manifest_diff["evidence_id"] in bundle.evidence_ids
    citable_hunk = next(fact for fact in bundle.facts if fact.evidence_id == hunk["evidence_id"])
    assert citable_hunk.value["path"] == "src/pkg.py"
    assert "VALUE = 2" in citable_hunk.value["diff"]
    assert hunk["evidence_id"] == context["code_hunks"][0]["evidence_id"]
    model_fact = next(
        fact for fact in bundle.model_view()["facts"] if fact["evidence_id"] == hunk["evidence_id"]
    )
    assert "VALUE = 2" in model_fact["value"]["diff"]


def test_maximum_selected_hunk_remains_literal_in_the_model_view():
    inspector = ArchiveInspector()
    before_text = "\n".join(f"VALUE_{index} = 'old'" for index in range(500))
    after_text = "\n".join(f"VALUE_{index} = 'new'" for index in range(500))
    before = inspector.inspect(
        "pkg-1.0.0.tar.gz",
        archive({"pkg-1.0.0/src/pkg.py": before_text}),
    )
    after = inspector.inspect(
        "pkg-2.0.0.tar.gz",
        archive({"pkg-2.0.0/src/pkg.py": after_text}),
    )
    context = diff_snapshots(
        before,
        after,
        baseline_version="1.0.0",
        candidate_version="2.0.0",
    )
    bundle = MetadataEvidenceBuilder().build(
        event(package="pkg"),
        {
            "info": {"name": "pkg", "version": "1.0.0"},
            "urls": [],
            "vulnerabilities": [],
        },
        {
            "info": {"name": "pkg", "version": "2.0.0"},
            "urls": [],
            "vulnerabilities": [],
        },
        context=context,
    )

    hunk = context["code_hunks"][0]
    model_fact = next(
        fact for fact in bundle.model_view()["facts"] if fact["evidence_id"] == hunk["evidence_id"]
    )
    assert hunk["truncated"] is True
    assert len(hunk["diff"]) == 4_000
    assert model_fact["value"]["diff"] == hunk["diff"]


def test_generated_metadata_is_ignored_only_when_version_is_the_only_change():
    inspector = ArchiveInspector()
    before = inspector.inspect(
        "pkg-1.0.0.tar.gz",
        archive({"pkg-1.0.0/PKG-INFO": "Name: pkg\nVersion: 1.0.0\nSummary: old\n"}),
    )
    after = inspector.inspect(
        "pkg-2.0.0.tar.gz",
        archive({"pkg-2.0.0/PKG-INFO": "Name: pkg\nVersion: 2.0.0\nSummary: new\n"}),
    )

    result = diff_snapshots(
        before,
        after,
        baseline_version="1.0.0",
        candidate_version="2.0.0",
    )

    assert result["artifact_manifest_diff"]["ignored_version_only"] == []
    assert result["artifact_manifest_diff"]["changed"] == ["PKG-INFO"]


def test_version_only_generated_metadata_does_not_force_model_routing():
    inspector = ArchiveInspector()
    before = inspector.inspect(
        "pkg-1.0.0.tar.gz",
        archive({"pkg-1.0.0/PKG-INFO": "Name: pkg\nVersion: 1.0.0\n"}),
    )
    after = inspector.inspect(
        "pkg-2.0.0.tar.gz",
        archive({"pkg-2.0.0/PKG-INFO": "Name: pkg\nVersion: 2.0.0\n"}),
    )
    context = diff_snapshots(
        before,
        after,
        baseline_version="1.0.0",
        candidate_version="2.0.0",
    )
    release_event = event(package="pkg")
    bundle = MetadataEvidenceBuilder().build(
        release_event,
        {
            "info": {"name": "pkg", "version": "1.0.0", "requires_python": ">=3.9"},
            "urls": [
                {
                    "filename": "pkg-1.0.0.tar.gz",
                    "packagetype": "sdist",
                    "digests": {"sha256": before.archive_sha256},
                }
            ],
            "vulnerabilities": [],
        },
        {
            "info": {"name": "pkg", "version": "2.0.0", "requires_python": ">=3.9"},
            "urls": [
                {
                    "filename": "pkg-2.0.0.tar.gz",
                    "packagetype": "sdist",
                    "digests": {"sha256": after.archive_sha256},
                }
            ],
            "vulnerabilities": [],
        },
        context=context,
    )

    decision = route(is_prerelease=False, evidence=bundle)
    assert decision.analysis_eligibility == "deterministic_non_substantive"
    assert context["artifact_manifest_diff"]["changed"] == []
    assert context["artifact_manifest_diff"]["ignored_version_only"] == ["PKG-INFO"]


def test_artifact_credentials_do_not_cross_evidence_or_model_boundaries():
    secrets = [
        "sk-proj-" + ("Fa0" * 30),
        "github_pat_11" + ("Ga0" * 24),
        "inert-private-material",
    ]
    private_key_begin = "-----BEGIN PRIVATE " + "KEY-----"
    private_key_end = "-----END PRIVATE " + "KEY-----"
    inspector = ArchiveInspector()
    before = inspector.inspect(
        "pkg-1.whl",
        archive({"pkg/main.py": "VALUE = 1\n", "README.md": "old\n"}),
    )
    after = inspector.inspect(
        "pkg-2.whl",
        archive(
            {
                "pkg/main.py": f'API_TOKEN = "{secrets[1]}"\n',
                "README.md": (
                    f"Provider token: {secrets[0]}\n"
                    f"{private_key_begin}\n"
                    f"{secrets[2]}\n"
                    f"{private_key_end}\n"
                ),
            }
        ),
    )
    raw_context = diff_snapshots(before, after)
    bundle = MetadataEvidenceBuilder().build(
        event(),
        {
            "info": {
                "version": "1.9.0",
                "requires_python": ">=3.9",
                "requires_dist": ["support-lib>=1"],
            },
            "urls": [],
            "vulnerabilities": [],
        },
        {
            "info": {
                "version": "2.0.0",
                "requires_python": ">=3.10",
                "requires_dist": ["support-lib>=2"],
            },
            "urls": [],
            "vulnerabilities": [],
        },
        context=raw_context,
    )
    provider = FakeModelProvider(
        [
            FakeOutcome(
                parsed=substantive(
                    evidence_id="computed.requires_python_diff.after",
                ),
            )
        ]
    )
    MaterialityEngine(provider).evaluate(
        bundle,
        route(is_prerelease=False, evidence=bundle),
    )

    rendered_boundaries = (
        repr(bundle.to_dict()),
        repr(bundle.model_view()),
        repr(provider.requests[0].model_input),
    )
    for rendered in rendered_boundaries:
        for secret in secrets:
            if secret in rendered:
                pytest.fail(
                    "an artifact credential crossed a sanitized boundary",
                    pytrace=False,
                )
    assert provider.requests[0].model_input["evidence"]["sanitization"] == bundle.sanitization
    assert bundle.sanitization["category_counts"] == {
        "basic_auth": 0,
        "bearer_token": 0,
        "github_token": 1,
        "openai_token": 1,
        "private_key": 1,
        "secret_field": 0,
        "url_credential": 0,
    }


def test_artifact_rejects_path_traversal_without_extracting():
    with pytest.raises(UnsafeArtifact, match="unsafe path"):
        ArchiveInspector().inspect("bad.whl", archive({"../escape.py": "bad"}))


def test_artifact_rejects_file_count_and_total_uncompressed_bounds():
    with pytest.raises(UnsafeArtifact, match="file-count"):
        ArchiveInspector(ArtifactPolicy(max_files=1)).inspect(
            "many.whl", archive({"a.py": "a", "b.py": "b"})
        )
    with pytest.raises(UnsafeArtifact, match="uncompressed-size"):
        ArchiveInspector(ArtifactPolicy(max_total_uncompressed_bytes=3)).inspect(
            "large.whl", archive({"a.py": "abcd"})
        )


def test_artifact_rejects_high_compression_ratio():
    with pytest.raises(UnsafeArtifact, match="compression-ratio"):
        ArchiveInspector(ArtifactPolicy(max_compression_ratio=2)).inspect(
            "bomb.whl", archive({"large.txt": "0" * 10_000})
        )
