"""Compile conclusive package metadata changes into deterministic findings."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from packaging.tags import Tag
from packaging.utils import parse_wheel_filename

from .models import EvidenceBundle, Json

RULE_VERSION = "deterministic-impact-v1"

_PYTHON_FLOOR = re.compile(r"^>=\s*(\d+)\.(\d+)$")
_CPYTHON_TAG = re.compile(r"^cp(\d)(\d+)$")
_MACOS_TAG = re.compile(r"^macosx_(\d+)_(\d+)_(.+)$")


@dataclass(frozen=True)
class DeterministicImpactCompilation:
    """Return a complete deterministic decision and its routing reasons."""

    gate_results: Json
    route_reasons: tuple[str, ...]


@dataclass(frozen=True)
class _Impact:
    dimension: str
    certainty: str
    headline: str
    affected_if: str
    changed_behavior: str
    observable_outcome: str
    recommended_action: str
    verification: str
    not_affected_if: str
    evidence_ids: tuple[str, ...]
    conditions: tuple[str, ...]
    change_type: str
    impact_type: str

    def proof(self, index: int) -> Json:
        return {
            "impact_id": f"impact-{index}",
            "dimension": self.dimension,
            "certainty": self.certainty,
            "headline": self.headline,
            "affected_if": self.affected_if,
            "what_happens": f"{self.changed_behavior} {self.observable_outcome}",
            "recommended_action": self.recommended_action,
            "verification": self.verification,
            "evidence_ids": list(self.evidence_ids),
        }


def compile_deterministic_impact(
    evidence: EvidenceBundle,
) -> DeterministicImpactCompilation | None:
    """Compile customer-visible impact from bounded PyPI metadata and wheel facts.

    A successful result is deliberately scoped to release selection and distribution
    availability. Dependency, vulnerability, and ambiguous specifier changes continue
    to the model-assisted path.
    """
    if evidence.collection_status != "complete" or evidence.computed.get("missing"):
        return None
    if evidence.computed.get("requires_dist_diff") or evidence.computed.get("vulnerability_diff"):
        return None

    package = evidence.package
    baseline_version = str(evidence.baseline.get("version") or "the prior release")
    candidate_version = str(evidence.candidate.get("version") or "the candidate release")
    impacts: list[_Impact] = []
    support_expansions: list[Json] = []
    excluded_python: set[str] = set()

    python_result = _python_compatibility(
        evidence, package=package, candidate_version=candidate_version
    )
    if python_result is None and evidence.computed.get("requires_python_diff"):
        return None
    if python_result:
        impact, expansion, excluded_python = python_result
        if impact:
            impacts.append(impact)
        if expansion:
            support_expansions.append(expansion)

    wheel_result = _wheel_compatibility(
        evidence,
        package=package,
        candidate_version=candidate_version,
        excluded_python=excluded_python,
    )
    if wheel_result is None and evidence.computed.get("files_diff"):
        return None
    if wheel_result:
        wheel_impacts, wheel_expansions = wheel_result
        impacts.extend(wheel_impacts)
        support_expansions.extend(wheel_expansions)

    yank_result = _yank_impact(evidence, package=package, candidate_version=candidate_version)
    if yank_result:
        impact, expansion = yank_result
        if impact:
            impacts.insert(0, impact)
        if expansion:
            support_expansions.append(expansion)

    if not impacts and not support_expansions:
        return None

    limitations = [
        "This deterministic conclusion covers PyPI release metadata and published distribution artifacts.",
        "Source-level runtime behavior and undocumented changes were not evaluated by these rules.",
    ]
    primary = (
        impacts[0]
        if impacts
        else _expansion_as_primary(
            support_expansions[0], package=package, candidate_version=candidate_version
        )
    )
    claims = [
        {
            "statement": impact.changed_behavior,
            "evidence_ids": list(impact.evidence_ids),
            "support": impact.certainty,
            "conditions": list(impact.conditions),
        }
        for impact in impacts
    ]
    claims.extend(
        {
            "statement": str(expansion["summary"]),
            "evidence_ids": list(expansion["evidence_ids"]),
            "support": "direct",
            "conditions": [],
        }
        for expansion in support_expansions
    )
    change_types = list(
        dict.fromkeys(
            [impact.change_type for impact in impacts]
            + [str(expansion["change_type"]) for expansion in support_expansions]
        )
    )
    primary_effect = f"{primary.changed_behavior} {primary.observable_outcome}"
    customer_summary = {
        "decision": "publishable_summary",
        "impact_type": primary.impact_type,
        "headline": primary.headline,
        "affected_if": primary.affected_if,
        "what_happens": primary_effect,
        "not_affected_if": primary.not_affected_if,
        "recommended_action": primary.recommended_action,
        "verification": primary.verification,
        "reach_summary": "This release belongs to an explicitly monitored package.",
        "evidence_ids": list(primary.evidence_ids),
        "limitations": limitations,
        "decision_card": {
            "headline": primary.headline,
            "applies_when": primary.affected_if,
            "action": primary.recommended_action,
            "source_scenario_id": "scenario-1",
        },
    }
    proof = {
        "decision": "impact_detected" if impacts else "support_expanded",
        "rule_version": RULE_VERSION,
        "evidence_bundle_id": evidence.bundle_id,
        "scope": "pypi_release_selection_and_distribution_availability",
        "baseline_version": baseline_version,
        "candidate_version": candidate_version,
        "impacts": [impact.proof(index) for index, impact in enumerate(impacts, 1)],
        "support_expansions": support_expansions,
        "resolved_change_types": change_types,
        "limitations": limitations,
        "model_calls_avoided": 3,
    }
    gate_results = {
        "deterministic_impact": proof,
        "materiality": {
            "decision": "substantive",
            "change_types": change_types,
            "claims": claims,
            "missing_evidence": [],
            "confidence": 1.0,
        },
        "applicability": {
            "assessment": primary_effect,
            "consumer_scenarios": [
                {
                    "impact_kind": "metadata",
                    "package": package,
                    "candidate_version": candidate_version,
                    "consumer_trigger": primary.affected_if,
                    "changed_behavior": primary.changed_behavior,
                    "observable_outcome": primary.observable_outcome,
                    "verification": primary.verification,
                    "evidence_ids": list(primary.evidence_ids),
                    "conditions": list(primary.conditions),
                }
            ],
            "confidence": 1.0,
            "limitations": limitations,
        },
        "customer_impact": {
            "valid": True,
            "errors": [],
            "customer_summary": customer_summary,
        },
    }
    return DeterministicImpactCompilation(
        gate_results=gate_results,
        route_reasons=(
            "explicitly_monitored_package",
            "complete_structured_evidence",
            "deterministic_release_impact",
        ),
    )


def _python_compatibility(
    evidence: EvidenceBundle, *, package: str, candidate_version: str
) -> tuple[_Impact | None, Json | None, set[str]] | None:
    diff = evidence.computed.get("requires_python_diff")
    if not diff:
        return None
    if not isinstance(diff, dict):
        return None
    before = diff.get("before")
    after = diff.get("after")
    if not isinstance(before, str) or not isinstance(after, str):
        return None
    old_floor = _parse_python_floor(before)
    new_floor = _parse_python_floor(after)
    if old_floor is None or new_floor is None or old_floor == new_floor:
        return None
    evidence_ids = (
        "computed.requires_python_diff.before",
        "computed.requires_python_diff.after",
    )
    if new_floor > old_floor:
        removed = _minor_range(old_floor, new_floor)
        if not removed:
            return None
        values = tuple(f"Python {major}.{minor}" for major, minor in removed)
        display = _display_values(values)
        affected_if = f"You install {package} {candidate_version} on {display}."
        return (
            _Impact(
                dimension="python_version",
                certainty="direct",
                headline=f"{package} {candidate_version} raises Python minimum",
                affected_if=affected_if,
                changed_behavior=f"Requires-Python changed from {before} to {after}.",
                observable_outcome="A metadata-aware installer excludes this release.",
                recommended_action="Stay on the prior release or upgrade Python before adopting it.",
                verification=(
                    f"Run python -m pip install --dry-run {package}=={candidate_version} "
                    f"using {display}."
                ),
                not_affected_if=f"Your interpreter satisfies Requires-Python {after}.",
                evidence_ids=evidence_ids,
                conditions=(affected_if,),
                change_type="python_compatibility",
                impact_type="install_block",
            ),
            None,
            {f"{major}.{minor}" for major, minor in removed},
        )
    added = _minor_range(new_floor, old_floor)
    if not added:
        return None
    values = [f"Python {major}.{minor}" for major, minor in added]
    return (
        None,
        {
            "dimension": "python_version",
            "values": values,
            "summary": f"Requires-Python now includes {_display_values(tuple(values))}.",
            "evidence_ids": list(evidence_ids),
            "change_type": "python_compatibility",
        },
        set(),
    )


def _parse_python_floor(value: str) -> tuple[int, int] | None:
    parts = [part.strip() for part in value.split(",") if part.strip()]
    floors = [match for part in parts if (match := _PYTHON_FLOOR.fullmatch(part))]
    other = [part for part in parts if not _PYTHON_FLOOR.fullmatch(part)]
    if len(floors) != 1 or other:
        return None
    return int(floors[0].group(1)), int(floors[0].group(2))


def _minor_range(start: tuple[int, int], end: tuple[int, int]) -> tuple[tuple[int, int], ...]:
    if start[0] != end[0] or end[1] - start[1] not in range(1, 11):
        return ()
    return tuple((start[0], minor) for minor in range(start[1], end[1]))


def _wheel_compatibility(
    evidence: EvidenceBundle,
    *,
    package: str,
    candidate_version: str,
    excluded_python: set[str],
) -> tuple[list[_Impact], list[Json]] | None:
    if not evidence.computed.get("files_diff"):
        return ([], [])
    baseline = _wheel_tags(evidence.baseline.get("files"), "baseline")
    candidate = _wheel_tags(evidence.candidate.get("files"), "candidate")
    if baseline is None or candidate is None:
        return None
    baseline_tags, baseline_sdists = baseline
    candidate_tags, candidate_sdists = candidate
    removed = set(baseline_tags) - set(candidate_tags)
    added = set(candidate_tags) - set(baseline_tags)
    impacts: list[_Impact] = []
    expansions: list[Json] = []

    for tag in tuple(removed):
        version = _python_from_interpreter(tag.interpreter)
        if version in excluded_python:
            removed.discard(tag)

    mac_changes = _macos_target_changes(removed, added)
    for old, new, arch, old_tags, new_tags in mac_changes:
        removed.difference_update(old_tags)
        added.difference_update(new_tags)
        arch_label = _architecture_label(arch)
        affected_if = (
            f"You install {package} {candidate_version} on macOS {arch_label} before {new}."
        )
        evidence_ids = _tag_evidence_ids(old_tags, baseline_tags) + _tag_evidence_ids(
            new_tags, candidate_tags
        )
        impacts.append(
            _Impact(
                dimension="platform",
                certainty="conditional",
                headline=f"{package} {candidate_version} raises macOS target",
                affected_if=affected_if,
                changed_behavior=f"Published macOS {arch_label} wheels now target {new} instead of {old}.",
                observable_outcome="Older systems may need a source build or may be unable to install the release.",
                recommended_action="Verify installation on the oldest supported macOS target before upgrading.",
                verification=f"Inspect or install the {package}=={candidate_version} wheel on the target macOS version.",
                not_affected_if=f"Your macOS {arch_label} environment is {new} or newer, or a compatible source build succeeds.",
                evidence_ids=tuple(dict.fromkeys(evidence_ids)),
                conditions=(affected_if,),
                change_type="platform_installability",
                impact_type="platform_installability",
            )
        )

    if removed:
        labels = tuple(sorted({_tag_label(tag) for tag in removed}))
        display = _display_values(labels)
        affected_if = f"You rely on a prebuilt wheel for {display}."
        evidence_ids = _tag_evidence_ids(removed, baseline_tags)
        impacts.append(
            _Impact(
                dimension="wheel_coverage",
                certainty="conditional",
                headline=f"{package} {candidate_version} removes wheel coverage",
                affected_if=affected_if,
                changed_behavior=f"The candidate no longer publishes wheel compatibility for {display}.",
                observable_outcome="Installation may fall back to a source build or fail without build tooling.",
                recommended_action="Test installation in the affected environment before upgrading.",
                verification=f"Run python -m pip install --only-binary=:all: {package}=={candidate_version} in that environment.",
                not_affected_if="A compatible candidate wheel exists or a source build is acceptable.",
                evidence_ids=tuple(dict.fromkeys(evidence_ids)),
                conditions=(affected_if,),
                change_type="platform_installability",
                impact_type="platform_installability",
            )
        )

    if baseline_sdists and not candidate_sdists:
        evidence_id = next(iter(baseline_sdists.values()))
        affected_if = f"You need to build {package} {candidate_version} from source."
        impacts.append(
            _Impact(
                dimension="source_distribution",
                certainty="direct",
                headline=f"{package} {candidate_version} removes source archive",
                affected_if=affected_if,
                changed_behavior="The candidate no longer publishes a source distribution on PyPI.",
                observable_outcome="Source-only installation workflows cannot select a PyPI source archive.",
                recommended_action="Remain on the prior release or use a compatible published wheel.",
                verification=f"Check the PyPI files list for {package} {candidate_version}.",
                not_affected_if="Your environment can install one of the published wheels.",
                evidence_ids=(evidence_id,),
                conditions=(affected_if,),
                change_type="platform_installability",
                impact_type="platform_installability",
            )
        )

    if added:
        python_values = sorted(
            {
                f"Python {version} wheels"
                for tag in added
                if (version := _python_from_interpreter(tag.interpreter)) is not None
                and not any(
                    _python_from_interpreter(old.interpreter) == version for old in baseline_tags
                )
            }
        )
        baseline_platforms = {_platform_identity(tag.platform) for tag in baseline_tags}
        platform_values = sorted(
            {
                _platform_label(tag.platform)
                for tag in added
                if _platform_identity(tag.platform) not in baseline_platforms
            }
        )
        values = python_values + platform_values
        if values:
            expansions.append(
                {
                    "dimension": "wheel_coverage",
                    "values": values,
                    "summary": f"Published wheel support now includes {_display_values(tuple(values))}.",
                    "evidence_ids": list(dict.fromkeys(_tag_evidence_ids(added, candidate_tags)))[
                        :8
                    ],
                    "change_type": "platform_installability",
                }
            )
    if candidate_sdists and not baseline_sdists:
        expansions.append(
            {
                "dimension": "source_distribution",
                "values": ["source distribution"],
                "summary": "The candidate adds a PyPI source distribution.",
                "evidence_ids": [next(iter(candidate_sdists.values()))],
                "change_type": "platform_installability",
            }
        )
    return impacts, expansions


def _wheel_tags(files: Any, prefix: str) -> tuple[dict[Tag, str], dict[str, str]] | None:
    if not isinstance(files, list):
        return None
    tags: dict[Tag, str] = {}
    sdists: dict[str, str] = {}
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            return None
        filename = item.get("filename")
        package_type = item.get("packagetype")
        if not isinstance(filename, str) or not isinstance(package_type, str):
            return None
        evidence_id = f"{prefix}.files.{index}.filename"
        if package_type == "bdist_wheel" and filename.endswith(".whl"):
            try:
                _, _, _, parsed = parse_wheel_filename(filename)
            except ValueError:
                return None
            for tag in parsed:
                tags[tag] = evidence_id
        elif package_type == "sdist":
            sdists[filename] = evidence_id
        else:
            return None
    return tags, sdists


def _macos_target_changes(
    removed: set[Tag], added: set[Tag]
) -> list[tuple[str, str, str, set[Tag], set[Tag]]]:
    grouped_old: dict[tuple[str, str, str], dict[tuple[int, int], set[Tag]]] = {}
    grouped_new: dict[tuple[str, str, str], dict[tuple[int, int], set[Tag]]] = {}
    for source, target in ((removed, grouped_old), (added, grouped_new)):
        for tag in source:
            match = _MACOS_TAG.fullmatch(tag.platform)
            if not match:
                continue
            key = (tag.interpreter, tag.abi, match.group(3))
            version = (int(match.group(1)), int(match.group(2)))
            target.setdefault(key, {}).setdefault(version, set()).add(tag)
    combined: dict[tuple[tuple[int, int], tuple[int, int], str], tuple[set[Tag], set[Tag]]] = {}
    for key in set(grouped_old) & set(grouped_new):
        old_version = min(grouped_old[key])
        new_version = min(grouped_new[key])
        if new_version <= old_version:
            continue
        combined_key = (old_version, new_version, key[2])
        old_tags, new_tags = combined.setdefault(combined_key, (set(), set()))
        old_tags.update(grouped_old[key][old_version])
        new_tags.update(grouped_new[key][new_version])
    return [
        (
            f"{old[0]}.{old[1]}",
            f"{new[0]}.{new[1]}",
            arch,
            old_tags,
            new_tags,
        )
        for (old, new, arch), (old_tags, new_tags) in sorted(combined.items())
    ]


def _tag_evidence_ids(tags: Any, mapping: dict[Tag, str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(mapping[tag] for tag in sorted(tags, key=str)))[:8]


def _python_from_interpreter(interpreter: str) -> str | None:
    match = _CPYTHON_TAG.fullmatch(interpreter)
    if not match:
        return None
    return f"{match.group(1)}.{int(match.group(2))}"


def _platform_family(platform: str) -> str:
    if platform.startswith("macosx_"):
        return "macos"
    if platform.startswith("ios_"):
        return "ios"
    if platform.startswith(("manylinux", "musllinux", "linux_")):
        return "linux"
    if platform.startswith("win"):
        return "windows"
    return platform


def _platform_label(platform: str) -> str:
    family = _platform_family(platform)
    architecture = _platform_architecture(platform)
    architecture_suffix = f" {_architecture_label(architecture)}" if architecture else ""
    if family == "ios":
        return f"iOS{architecture_suffix} wheels"
    if family == "macos":
        return f"macOS{architecture_suffix} wheels"
    if family == "linux":
        return f"Linux{architecture_suffix} wheels"
    if family == "windows":
        return f"Windows{architecture_suffix} wheels"
    return f"{platform} wheels"


def _platform_identity(platform: str) -> tuple[str, str | None]:
    return _platform_family(platform), _platform_architecture(platform)


def _platform_architecture(platform: str) -> str | None:
    for architecture in (
        "x86_64",
        "aarch64",
        "arm64",
        "ppc64le",
        "s390x",
        "amd64",
        "i686",
        "i386",
        "win32",
    ):
        if architecture in platform:
            return architecture
    return None


def _tag_label(tag: Tag) -> str:
    python = _python_from_interpreter(tag.interpreter)
    prefix = f"Python {python} " if python else f"{tag.interpreter} "
    return f"{prefix}{_platform_label(tag.platform)}"


def _architecture_label(value: str) -> str:
    return {
        "x86_64": "x86-64",
        "amd64": "x86-64",
        "arm64": "ARM64",
        "aarch64": "ARM64",
    }.get(value, value.replace("_", "-"))


def _yank_impact(
    evidence: EvidenceBundle, *, package: str, candidate_version: str
) -> tuple[_Impact | None, Json | None] | None:
    diff = evidence.computed.get("yank_diff")
    if not diff:
        return None
    if not isinstance(diff, dict) or not isinstance(diff.get("after"), bool):
        return None
    evidence_ids = ("computed.yank_diff.before", "computed.yank_diff.after")
    if diff["after"]:
        affected_if = f"Your resolver is considering {package} {candidate_version}."
        return (
            _Impact(
                dimension="release_availability",
                certainty="direct",
                headline=f"{package} {candidate_version} is yanked",
                affected_if=affected_if,
                changed_behavior="The candidate release is marked as yanked on PyPI.",
                observable_outcome="Installers normally avoid selecting it unless it is pinned exactly.",
                recommended_action="Select a non-yanked release unless this version is intentionally pinned.",
                verification=f"Inspect the PyPI release status for {package} {candidate_version}.",
                not_affected_if="Your resolver selects a different, non-yanked version.",
                evidence_ids=evidence_ids,
                conditions=(affected_if,),
                change_type="release_withdrawal",
                impact_type="release_withdrawal",
            ),
            None,
        )
    return (
        None,
        {
            "dimension": "release_availability",
            "values": [candidate_version],
            "summary": f"{package} {candidate_version} is no longer marked as yanked.",
            "evidence_ids": list(evidence_ids),
            "change_type": "release_availability",
        },
    )


def _expansion_as_primary(expansion: Json, *, package: str, candidate_version: str) -> _Impact:
    summary = str(expansion["summary"])
    if expansion["dimension"] == "release_availability":
        affected_if = f"You want to install {package} {candidate_version}."
        return _Impact(
            dimension="release_availability",
            certainty="direct",
            headline=f"{package} {candidate_version} is available again",
            affected_if=affected_if,
            changed_behavior=summary,
            observable_outcome=(
                "Installers may select it again when version constraints are compatible."
            ),
            recommended_action=(
                "Re-evaluate any temporary pin or exclusion before adopting this release."
            ),
            verification=f"Inspect the PyPI release status for {package} {candidate_version}.",
            not_affected_if="Your resolver selects a different version.",
            evidence_ids=tuple(str(item) for item in expansion["evidence_ids"]),
            conditions=(affected_if,),
            change_type=str(expansion["change_type"]),
            impact_type="release_availability",
        )
    affected_if = f"You want to use {package} {candidate_version} on the newly supported target."
    return _Impact(
        dimension=str(expansion["dimension"]),
        certainty="direct",
        headline=f"{package} {candidate_version} expands support",
        affected_if=affected_if,
        changed_behavior=summary,
        observable_outcome="A compatible published distribution may now be selected.",
        recommended_action="Review the new support if adopting this release on that target.",
        verification=f"Inspect the published files for {package} {candidate_version} on PyPI.",
        not_affected_if="You do not use the newly supported target.",
        evidence_ids=tuple(str(item) for item in expansion["evidence_ids"]),
        conditions=(affected_if,),
        change_type=str(expansion["change_type"]),
        impact_type="platform_installability",
    )


def _display_values(values: tuple[str, ...]) -> str:
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    visible = values[:3]
    suffix = f", and {len(values) - 3} more" if len(values) > 3 else f", and {visible[-1]}"
    if len(values) == 3:
        return f"{visible[0]}, {visible[1]}, and {visible[2]}"
    return f"{visible[0]}, {visible[1]}, {visible[2]}{suffix}"
