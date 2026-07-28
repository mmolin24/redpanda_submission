"""Build bounded, normalized evidence for deterministic and model analysis."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .artifacts import ArchiveInspector, UnsafeArtifact, diff_snapshots
from .ids import sha256_json
from .models import (
    EvidenceBundle,
    EvidenceCollectionStatus,
    EvidenceFact,
    EvidenceSource,
    Json,
    ReleaseEvent,
    utc_now,
)
from .sanitization import sanitize_with_report

_OMITTED_METADATA_KEYS = {
    "author",
    "author_email",
    "maintainer",
    "maintainer_email",
    "description",
    "description_content_type",
    "summary",
}


def _stable_provenance_identity(value: Any) -> Any:
    """Exclude observation time while retaining provenance content identity."""
    if isinstance(value, dict):
        return {
            key: _stable_provenance_identity(item)
            for key, item in value.items()
            if key != "retrieved_at"
        }
    if isinstance(value, list):
        return [_stable_provenance_identity(item) for item in value]
    return value


def compact_metadata(info: Json) -> Json:
    """Retain only bounded release metadata used by downstream analysis."""
    allowed = {
        "name",
        "version",
        "requires_python",
        "requires_dist",
        "classifiers",
        "license_expression",
        "yanked",
        "yanked_reason",
    }
    return {k: v for k, v in info.items() if k in allowed and k not in _OMITTED_METADATA_KEYS}


def _file_facts(files: Iterable[Json]) -> list[Json]:
    return [
        {
            "filename": item.get("filename"),
            "packagetype": item.get("packagetype"),
            "python_version": item.get("python_version"),
            "size": item.get("size"),
            "sha256": (item.get("digests") or {}).get("sha256"),
            "yanked": item.get("yanked", False),
            "yanked_reason": item.get("yanked_reason"),
            "upload_time_iso_8601": item.get("upload_time_iso_8601"),
        }
        for item in files
    ]


def select_baseline_version(
    project: Json, candidate_version: str, candidate_uploaded_at: str | None
) -> str | None:
    """Select the highest non-yanked final release uploaded before the candidate."""
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError as exc:  # pragma: no cover - dependency setup guard
        raise RuntimeError("packaging is required for baseline selection") from exc

    candidate_time = _parse_time(candidate_uploaded_at)
    candidates: list[Version] = []
    releases = project.get("releases", {})
    for raw_version, files in releases.items():
        if raw_version == candidate_version or not files:
            continue
        try:
            version = Version(raw_version)
        except InvalidVersion:
            continue
        if version.is_prerelease or all(bool(f.get("yanked")) for f in files):
            continue
        upload_times = [
            parsed
            for f in files
            if (parsed := _parse_time(f.get("upload_time_iso_8601"))) is not None
        ]
        earliest = min(upload_times, default=None)
        if candidate_time is not None and (earliest is None or earliest >= candidate_time):
            continue
        candidates.append(version)
    return str(max(candidates)) if candidates else None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _requires_dist_map(values: list[str] | None) -> dict[str, str]:
    if not values:
        return {}
    try:
        from packaging.requirements import InvalidRequirement, Requirement
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("packaging is required for dependency evidence") from exc

    result: dict[str, str] = {}
    for raw in values:
        try:
            parsed = Requirement(raw)
        except InvalidRequirement:
            result[f"invalid:{sha256_json(raw)[7:19]}"] = raw
            continue
        result[parsed.name.lower().replace("_", "-")] = str(parsed)
    return result


@dataclass(frozen=True)
class MetadataEvidenceBuilder:
    """Turns already-fetched exact-release data into an immutable evidence bundle."""

    def build(
        self,
        event: ReleaseEvent,
        baseline_response: Json,
        candidate_response: Json,
        *,
        context: Json | None = None,
        provenance: Iterable[Json] = (),
        collection_status: EvidenceCollectionStatus = "complete",
    ) -> EvidenceBundle:
        baseline_info = compact_metadata(baseline_response.get("info", {}))
        candidate_info = compact_metadata(candidate_response.get("info", {}))
        baseline_files = _file_facts(baseline_response.get("urls", []))
        candidate_files = _file_facts(candidate_response.get("urls", []))
        baseline_dist = _requires_dist_map(baseline_info.get("requires_dist"))
        candidate_dist = _requires_dist_map(candidate_info.get("requires_dist"))
        dependency_names = sorted(set(baseline_dist) | set(candidate_dist))

        requires_python_diff = {
            "before": baseline_info.get("requires_python"),
            "after": candidate_info.get("requires_python"),
        }
        files_diff = {
            "added": sorted(
                {str(f.get("filename")) for f in candidate_files}
                - {str(f.get("filename")) for f in baseline_files}
            ),
            "removed": sorted(
                {str(f.get("filename")) for f in baseline_files}
                - {str(f.get("filename")) for f in candidate_files}
            ),
        }
        yank_diff = {
            "before": any(bool(f.get("yanked")) for f in baseline_files),
            "after": any(bool(f.get("yanked")) for f in candidate_files),
        }
        vulnerability_diff = {
            "before_count": len(baseline_response.get("vulnerabilities", [])),
            "after_count": len(candidate_response.get("vulnerabilities", [])),
        }
        computed: Json = {
            "requires_python_diff": requires_python_diff
            if requires_python_diff["before"] != requires_python_diff["after"]
            else {},
            "requires_dist_diff": {
                name: {
                    "before": baseline_dist.get(name),
                    "after": candidate_dist.get(name),
                }
                for name in dependency_names
                if baseline_dist.get(name) != candidate_dist.get(name)
            },
            "files_diff": files_diff if files_diff["added"] or files_diff["removed"] else {},
            "yank_diff": yank_diff if yank_diff["before"] != yank_diff["after"] else {},
            "vulnerability_diff": vulnerability_diff
            if vulnerability_diff["before_count"] != vulnerability_diff["after_count"]
            else {},
            "missing": [],
        }
        baseline = {
            "version": baseline_info.get("version"),
            "metadata": baseline_info,
            "files": baseline_files,
        }
        candidate = {
            "version": candidate_info.get("version", event.release.version),
            "metadata": candidate_info,
            "files": candidate_files,
            "vulnerabilities": candidate_response.get("vulnerabilities", []),
        }
        sanitized = sanitize_with_report(
            {
                "baseline": baseline,
                "candidate": candidate,
                "computed": computed,
                "context": context or {},
                "provenance": list(provenance),
            }
        )
        safe_payload = sanitized.value
        if not isinstance(safe_payload, dict):
            raise RuntimeError("sanitization must preserve the evidence object root")
        safe_baseline = safe_payload["baseline"]
        safe_candidate = safe_payload["candidate"]
        safe_computed = safe_payload["computed"]
        safe_context = safe_payload["context"]
        safe_provenance = safe_payload["provenance"]
        if not all(
            isinstance(item, dict)
            for item in (safe_baseline, safe_candidate, safe_computed, safe_context)
        ) or not isinstance(safe_provenance, list):
            raise RuntimeError("sanitization must preserve evidence container types")
        sanitization_report = sanitized.report.to_dict()
        facts = self._facts(
            safe_baseline,
            safe_candidate,
            safe_computed,
            safe_context,
        )
        body = {
            "event_key": event.event_key,
            "package": event.package.normalized_name,
            "baseline": safe_baseline,
            "candidate": safe_candidate,
            "computed": safe_computed,
            "context": safe_context,
            # Retrieval time is useful audit data, but it is not evidence
            # content. Excluding it keeps replayed identical evidence stable.
            "provenance": _stable_provenance_identity(safe_provenance),
            "collection_status": collection_status,
            "sanitization": sanitization_report,
        }
        return EvidenceBundle(
            bundle_id=sha256_json(body),
            event_key=event.event_key,
            package=event.package.normalized_name,
            baseline=safe_baseline,
            candidate=safe_candidate,
            computed=safe_computed,
            context=safe_context,
            provenance=tuple(safe_provenance),
            collection_status=collection_status,
            facts=tuple(facts),
            sanitization=sanitization_report,
        )

    def _facts(
        self, baseline: Json, candidate: Json, computed: Json, context: Json
    ) -> list[EvidenceFact]:
        result: list[EvidenceFact] = []
        sources: tuple[tuple[EvidenceSource, Json], ...] = (
            ("baseline", baseline),
            ("candidate", candidate),
            ("computed", computed),
            ("context", context),
        )
        for prefix, data in sources:
            fact_iterator = (
                _context_facts(data, prefix) if prefix == "context" else _leaf_facts(data, prefix)
            )
            for path, value in fact_iterator:
                if value not in (None, "", [], {}):
                    result.append(EvidenceFact(path, value, prefix))
        evidence_ids = [fact.evidence_id for fact in result]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise RuntimeError("evidence identifiers must be unique")
        return result


def _leaf_facts(value: Any, path: str) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key in sorted(value):
            yield from _leaf_facts(value[key], f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _leaf_facts(item, f"{path}.{index}")
    else:
        yield path, value


def _context_facts(value: Any, path: str) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        evidence_id = value.get("evidence_id")
        if isinstance(evidence_id, str) and evidence_id:
            yield (
                evidence_id,
                {key: item for key, item in value.items() if key != "evidence_id"},
            )
            return
        for key in sorted(value):
            yield from _context_facts(value[key], f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _context_facts(item, f"{path}.{index}")
    else:
        yield path, value


class JsonFetcher(Protocol):
    """Fetch one allowlisted JSON object."""

    def fetch_json(self, url: str) -> Json: ...


class ByteFetcher(Protocol):
    """Fetch a byte payload within a caller-provided limit."""

    def fetch_bytes(self, url: str, max_bytes: int) -> bytes: ...


class ExactReleaseNotFound(FileNotFoundError):
    """The release named by the source record no longer exists in PyPI."""


class PyPIResourceNotFound(FileNotFoundError):
    """An allowlisted PyPI HTTP request returned 404."""


def _validate_https_source(
    url: str,
    *,
    allowed_hosts: frozenset[str],
    error_message: str,
) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        raise ValueError(error_message)


class BoundedHttpJsonFetcher:
    """Fetch allowlisted PyPI JSON with bounded retries and response size."""

    def __init__(
        self,
        *,
        user_agent: str = "pypi-change-intelligence/0.1 local-assessment",
        timeout_seconds: float = 10,
        max_body_bytes: int = 5_000_000,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.max_body_bytes = max_body_bytes
        self.max_attempts = max_attempts
        self.sleep = sleep

    def fetch_json(self, url: str) -> Json:
        _validate_https_source(
            url,
            allowed_hosts=frozenset({"pypi.org"}),
            error_message="JSON URL is outside the PyPI allowlist",
        )
        last: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            # `_validate_https_source` rejects non-HTTPS and unreviewed hosts.
            request = urllib.request.Request(  # noqa: S310
                url,
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            )
            try:
                # Both the requested and final redirect hosts are allowlisted.
                with urllib.request.urlopen(  # noqa: S310
                    request, timeout=self.timeout_seconds
                ) as response:
                    _validate_https_source(
                        response.geturl(),
                        allowed_hosts=frozenset({"pypi.org"}),
                        error_message="JSON redirect target is outside the PyPI allowlist",
                    )
                    body = response.read(self.max_body_bytes + 1)
                if len(body) > self.max_body_bytes:
                    raise ValueError("PyPI response exceeded body-size limit")
                value = json.loads(body)
                if not isinstance(value, dict):
                    raise ValueError("PyPI response was not a JSON object")
                return value
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise PyPIResourceNotFound("PyPI resource not found") from exc
                last = exc
                retryable = exc.code == 429 or exc.code >= 500
                if not retryable or attempt == self.max_attempts:
                    raise
            except (TimeoutError, urllib.error.URLError) as exc:
                last = exc
                if attempt == self.max_attempts:
                    raise
            self.sleep(0.25 * (2 ** (attempt - 1)))
        raise RuntimeError("PyPI request exhausted") from last


class BoundedHttpByteFetcher:
    """Fetch allowlisted PyPI artifacts with a hard response-size limit."""

    def __init__(
        self,
        *,
        user_agent: str = "pypi-change-intelligence/0.1 local-assessment",
        timeout_seconds: float = 15,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds

    def fetch_bytes(self, url: str, max_bytes: int) -> bytes:
        allowed_hosts = frozenset({"files.pythonhosted.org", "pypi.org"})
        _validate_https_source(
            url,
            allowed_hosts=allowed_hosts,
            error_message="artifact URL is outside the PyPI allowlist",
        )
        # `_validate_https_source` rejects non-HTTPS and unreviewed hosts.
        request = urllib.request.Request(  # noqa: S310
            url, headers={"User-Agent": self.user_agent}
        )
        # Both the requested and final redirect hosts are allowlisted.
        with urllib.request.urlopen(  # noqa: S310
            request, timeout=self.timeout_seconds
        ) as response:
            _validate_https_source(
                response.geturl(),
                allowed_hosts=allowed_hosts,
                error_message="artifact redirect target is outside the PyPI allowlist",
            )
            body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("artifact exceeds compressed size limit")
        return body


class PyPIEnricher:
    """Build candidate-versus-baseline evidence from public PyPI resources."""

    def __init__(
        self,
        fetcher: JsonFetcher,
        builder: MetadataEvidenceBuilder | None = None,
        *,
        byte_fetcher: ByteFetcher | None = None,
        archive_inspector: ArchiveInspector | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.builder = builder or MetadataEvidenceBuilder()
        self.byte_fetcher = byte_fetcher
        self.archive_inspector = archive_inspector or ArchiveInspector()

    def enrich(self, event: ReleaseEvent) -> EvidenceBundle:
        package = event.package.normalized_name
        candidate_url = f"https://pypi.org/pypi/{package}/{event.release.version}/json"
        project_url = f"https://pypi.org/pypi/{package}/json"
        try:
            candidate = self.fetcher.fetch_json(candidate_url)
        except PyPIResourceNotFound as exc:
            raise ExactReleaseNotFound from exc
        project = self.fetcher.fetch_json(project_url)
        uploads = [
            value
            for item in candidate.get("urls", [])
            if (value := item.get("upload_time_iso_8601"))
        ]
        baseline_version = select_baseline_version(
            project,
            event.release.version,
            min(uploads) if uploads else event.release.published_at,
        )
        if baseline_version is None:
            baseline = {"info": {"version": None}, "urls": [], "vulnerabilities": []}
            status = "partial"
        else:
            baseline_url = f"https://pypi.org/pypi/{package}/{baseline_version}/json"
            baseline = self.fetcher.fetch_json(baseline_url)
            status = "complete"
        provenance = [
            {
                "source_url": candidate_url,
                "content_sha256": sha256_json(candidate),
                "retrieved_at": utc_now(),
            },
            {
                "source_url": project_url,
                "content_sha256": sha256_json(project),
                "retrieved_at": utc_now(),
            },
        ]
        if baseline_version:
            provenance.append(
                {
                    "source_url": f"https://pypi.org/pypi/{package}/{baseline_version}/json",
                    "content_sha256": sha256_json(baseline),
                    "retrieved_at": utc_now(),
                }
            )
        context: Json = {"repository_mapping_confidence": "unavailable"}
        if baseline_version and self.byte_fetcher:
            baseline_artifact = _select_artifact(baseline.get("urls", []))
            candidate_artifact = _select_artifact(candidate.get("urls", []))
            if baseline_artifact and candidate_artifact:
                try:
                    before_bytes = self.byte_fetcher.fetch_bytes(
                        baseline_artifact["url"],
                        self.archive_inspector.policy.max_archive_bytes,
                    )
                    after_bytes = self.byte_fetcher.fetch_bytes(
                        candidate_artifact["url"],
                        self.archive_inspector.policy.max_archive_bytes,
                    )
                    before = self.archive_inspector.inspect(
                        baseline_artifact["filename"], before_bytes
                    )
                    after = self.archive_inspector.inspect(
                        candidate_artifact["filename"], after_bytes
                    )
                    context.update(
                        diff_snapshots(
                            before,
                            after,
                            baseline_version=baseline_version,
                            candidate_version=event.release.version,
                        )
                    )
                    provenance.extend(
                        [
                            {
                                "source_url": baseline_artifact["url"],
                                "content_sha256": before.archive_sha256,
                                "retrieved_at": utc_now(),
                            },
                            {
                                "source_url": candidate_artifact["url"],
                                "content_sha256": after.archive_sha256,
                                "retrieved_at": utc_now(),
                            },
                        ]
                    )
                except (OSError, ValueError, UnsafeArtifact, urllib.error.URLError):
                    status = "partial"
                    context["artifact_collection_error"] = (
                        "artifact context unavailable under safety bounds"
                    )
            else:
                status = "partial"
                context["artifact_collection_error"] = "comparable artifacts unavailable"
        elif baseline_version:
            status = "partial"
            context["artifact_collection_error"] = "artifact byte fetcher not configured"
        return self.builder.build(
            event,
            baseline,
            candidate,
            provenance=provenance,
            collection_status=status,
            context=context,
        )


def _select_artifact(files: list[Json]) -> Json | None:
    candidates = [
        item
        for item in files
        if item.get("url")
        and item.get("filename")
        and item.get("packagetype") in {"sdist", "bdist_wheel"}
    ]
    return min(
        candidates,
        key=lambda item: (
            0 if item.get("packagetype") == "sdist" else 1,
            str(item.get("filename")),
        ),
        default=None,
    )
