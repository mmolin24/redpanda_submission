"""Fetch, normalize, and compare release artifacts used as evidence."""

from __future__ import annotations

import difflib
import hashlib
import io
import posixpath
import re
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from .models import Json


class UnsafeArtifact(ValueError):
    """Reject an archive that violates inspection safety limits."""


@dataclass(frozen=True)
class ArtifactPolicy:
    """Bound archive inspection resource use and captured context."""

    max_archive_bytes: int = 20_000_000
    max_files: int = 2_000
    max_total_uncompressed_bytes: int = 50_000_000
    max_single_file_bytes: int = 750_000
    max_compression_ratio: int = 100
    max_context_bytes: int = 1_000_000


@dataclass(frozen=True)
class ArtifactSnapshot:
    """Hold a safe archive manifest and bounded text content."""

    filename: str
    archive_sha256: str
    manifest: tuple[Json, ...]
    text_files: dict[str, str]


@dataclass(frozen=True)
class _ComparisonView:
    snapshot: ArtifactSnapshot
    stripped_root: str | None
    manifest: dict[str, Json]
    text_files: dict[str, str]
    original_paths: dict[str, str]


class ArchiveInspector:
    """Bounded in-memory archive inspection; never extracts, imports, or executes."""

    def __init__(self, policy: ArtifactPolicy | None = None) -> None:
        self.policy = policy or ArtifactPolicy()

    def inspect(self, filename: str, payload: bytes) -> ArtifactSnapshot:
        if len(payload) > self.policy.max_archive_bytes:
            raise UnsafeArtifact("archive exceeds compressed size limit")
        if zipfile.is_zipfile(io.BytesIO(payload)):
            manifest, texts = self._zip(payload)
        elif tarfile.is_tarfile(io.BytesIO(payload)):
            manifest, texts = self._tar(payload)
        else:
            raise UnsafeArtifact("unsupported archive format")
        return ArtifactSnapshot(
            filename=filename,
            archive_sha256=hashlib.sha256(payload).hexdigest(),
            manifest=tuple(manifest),
            text_files=texts,
        )

    def _zip(self, payload: bytes) -> tuple[list[Json], dict[str, str]]:
        manifest: list[Json] = []
        texts: dict[str, str] = {}
        total = 0
        context_bytes = 0
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            if len(infos) > self.policy.max_files:
                raise UnsafeArtifact("archive file-count limit exceeded")
            for info in infos:
                if info.is_dir():
                    continue
                path = _safe_path(info.filename)
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise UnsafeArtifact("archive contains a symbolic link")
                total += info.file_size
                if total > self.policy.max_total_uncompressed_bytes:
                    raise UnsafeArtifact("archive uncompressed-size limit exceeded")
                if (
                    info.compress_size
                    and info.file_size / info.compress_size > self.policy.max_compression_ratio
                ):
                    raise UnsafeArtifact("archive compression-ratio limit exceeded")
                digest = None
                if info.file_size <= self.policy.max_single_file_bytes:
                    data = archive.read(info)
                    digest = hashlib.sha256(data).hexdigest()
                    context_bytes = self._capture_text(path, data, texts, context_bytes)
                manifest.append({"path": path, "size": info.file_size, "sha256": digest})
        return manifest, texts

    def _tar(self, payload: bytes) -> tuple[list[Json], dict[str, str]]:
        manifest: list[Json] = []
        texts: dict[str, str] = {}
        total = 0
        context_bytes = 0
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
            members = archive.getmembers()
            if len(members) > self.policy.max_files:
                raise UnsafeArtifact("archive file-count limit exceeded")
            for member in members:
                if member.isdir():
                    continue
                path = _safe_path(member.name)
                if not member.isfile():
                    raise UnsafeArtifact("archive contains a link or special file")
                total += member.size
                if total > self.policy.max_total_uncompressed_bytes:
                    raise UnsafeArtifact("archive uncompressed-size limit exceeded")
                digest = None
                if member.size <= self.policy.max_single_file_bytes:
                    stream = archive.extractfile(member)
                    data = stream.read(self.policy.max_single_file_bytes + 1) if stream else b""
                    digest = hashlib.sha256(data).hexdigest()
                    context_bytes = self._capture_text(path, data, texts, context_bytes)
                manifest.append({"path": path, "size": member.size, "sha256": digest})
        return manifest, texts

    def _capture_text(self, path: str, data: bytes, texts: dict[str, str], used: int) -> int:
        if used >= self.policy.max_context_bytes or not _is_context_file(path):
            return used
        remaining = self.policy.max_context_bytes - used
        bounded = data[:remaining]
        if b"\x00" in bounded:
            return used
        try:
            text = bounded.decode("utf-8")
        except UnicodeDecodeError:
            return used
        texts[path] = text
        return used + len(bounded)


def diff_snapshots(
    before: ArtifactSnapshot,
    after: ArtifactSnapshot,
    *,
    baseline_version: str | None = None,
    candidate_version: str | None = None,
    max_hunks: int = 20,
    max_hunk_chars: int = 4_000,
) -> Json:
    """Compare two artifact snapshots while suppressing version-only noise."""
    before_view = _comparison_view(before, baseline_version)
    after_view = _comparison_view(after, candidate_version)
    before_manifest = before_view.manifest
    after_manifest = after_view.manifest
    added = sorted(set(after_manifest) - set(before_manifest))
    removed = sorted(set(before_manifest) - set(after_manifest))
    changed_candidates = sorted(
        path
        for path in set(before_manifest) & set(after_manifest)
        if before_manifest[path].get("sha256") != after_manifest[path].get("sha256")
        or before_manifest[path].get("size") != after_manifest[path].get("size")
    )
    ignored_version_only = sorted(
        path
        for path in changed_candidates
        if _is_generated_version_only_change(
            path,
            before_view.text_files.get(path),
            after_view.text_files.get(path),
            baseline_version,
            candidate_version,
        )
    )
    changed = [path for path in changed_candidates if path not in ignored_version_only]
    manifest_identity = "\0".join(
        (
            *(f"added:{path}" for path in added),
            *(f"removed:{path}" for path in removed),
            *(f"changed:{path}" for path in changed),
            *(f"ignored_version_only:{path}" for path in ignored_version_only),
        )
    )
    hunks: list[Json] = []
    for path in sorted(changed, key=_diff_selection_key):
        if (
            len(hunks) >= max_hunks
            or _is_release_document(path)
            or path not in before_view.text_files
            or path not in after_view.text_files
        ):
            continue
        lines = list(
            difflib.unified_diff(
                before_view.text_files[path].splitlines(),
                after_view.text_files[path].splitlines(),
                fromfile=f"baseline/{path}",
                tofile=f"candidate/{path}",
                n=3,
                lineterm="",
            )
        )
        selected_lines = lines[:400]
        rendered = "\n".join(selected_lines)
        truncated = len(lines) > len(selected_lines) or len(rendered) > max_hunk_chars
        rendered = rendered[:max_hunk_chars]
        hunks.append(
            {
                "evidence_id": _stable_evidence_id("text-diff", before, after, path, rendered),
                "kind": "text_diff",
                "path": path,
                "baseline_path": before_view.original_paths[path],
                "candidate_path": after_view.original_paths[path],
                "baseline_sha256": before_manifest[path].get("sha256"),
                "candidate_sha256": after_manifest[path].get("sha256"),
                "diff": rendered,
                "selection_priority": _diff_selection_priority(path),
                "truncated": truncated,
            }
        )
    return {
        "artifact_comparison": {
            "policy_version": "artifact-comparison-v2",
            "baseline": {
                "filename": before.filename,
                "archive_sha256": before.archive_sha256,
                "stripped_root": before_view.stripped_root,
            },
            "candidate": {
                "filename": after.filename,
                "archive_sha256": after.archive_sha256,
                "stripped_root": after_view.stripped_root,
            },
        },
        "artifact_manifest_diff": {
            "evidence_id": _stable_evidence_id(
                "manifest-diff", before, after, "*", manifest_identity
            ),
            "kind": "artifact_manifest_diff",
            "added": added,
            "removed": removed,
            "changed": changed,
            "ignored_version_only": ignored_version_only,
            "selection_priority": 80,
        },
        "code_hunks": hunks,
        "documents": _document_changes(before_view, after_view),
    }


def _comparison_view(snapshot: ArtifactSnapshot, version: str | None) -> _ComparisonView:
    raw_paths = [str(item["path"]) for item in snapshot.manifest]
    stripped_root = _versioned_archive_root(snapshot.filename, raw_paths, version)
    manifest: dict[str, Json] = {}
    text_files: dict[str, str] = {}
    original_paths: dict[str, str] = {}
    for item in snapshot.manifest:
        original_path = str(item["path"])
        path = _comparison_path(original_path, stripped_root, version)
        if path in manifest:
            raise UnsafeArtifact("archive paths collide after comparison normalization")
        manifest[path] = {**item, "path": path, "original_path": original_path}
        original_paths[path] = original_path
    for original_path, content in snapshot.text_files.items():
        path = _comparison_path(original_path, stripped_root, version)
        if path in text_files:
            raise UnsafeArtifact("archive text paths collide after comparison normalization")
        text_files[path] = content
    return _ComparisonView(
        snapshot=snapshot,
        stripped_root=stripped_root,
        manifest=manifest,
        text_files=text_files,
        original_paths=original_paths,
    )


def _versioned_archive_root(filename: str, paths: list[str], version: str | None) -> str | None:
    if not version or not paths:
        return None
    parts = [PurePosixPath(path).parts for path in paths]
    roots = {item[0] for item in parts if len(item) > 1}
    if len(roots) != 1 or any(len(item) < 2 for item in parts):
        return None
    root = next(iter(roots))
    archive_stem = filename
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar", ".zip"):
        if archive_stem.endswith(suffix):
            archive_stem = archive_stem[: -len(suffix)]
            break
    canonical = lambda value: re.sub(r"[-_.]+", "-", value).casefold()  # noqa: E731
    if canonical(root) != canonical(archive_stem):
        return None
    version_pattern = rf"(?:^|[-_.]){re.escape(version)}$"
    return root if re.search(version_pattern, root, re.IGNORECASE) else None


def _comparison_path(path: str, root: str | None, version: str | None) -> str:
    parts = list(PurePosixPath(path).parts)
    if root and parts and parts[0] == root:
        parts = parts[1:]
    if version:
        parts = [
            part.replace(version, "<release-version>")
            if part.endswith((".dist-info", ".egg-info")) and version in part
            else part
            for part in parts
        ]
    return str(PurePosixPath(*parts))


def _stable_evidence_id(
    kind: str,
    before: ArtifactSnapshot,
    after: ArtifactSnapshot,
    path: str,
    detail: str,
) -> str:
    digest = hashlib.sha256()
    for value in (kind, before.archive_sha256, after.archive_sha256, path, detail):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return f"artifact.{kind}.sha256:{digest.hexdigest()}"


def _is_generated_version_only_change(
    path: str,
    before: str | None,
    after: str | None,
    baseline_version: str | None,
    candidate_version: str | None,
) -> bool:
    if not before or not after or not baseline_version or not candidate_version:
        return False
    if PurePosixPath(path).name.casefold() not in {"metadata", "pkg-info"}:
        return False

    def normalize(value: str, version: str) -> str:
        pattern = rf"(?m)^Version:\s*{re.escape(version)}\s*$"
        return re.sub(pattern, "Version: <release-version>", value)

    return normalize(before, baseline_version) == normalize(after, candidate_version)


def _diff_selection_priority(path: str) -> int:
    normalized = f"/{path.casefold()}"
    leaf = PurePosixPath(path).name.casefold()
    if _is_release_document(path):
        return 10
    if any(segment in {"test", "tests"} for segment in PurePosixPath(path).parts):
        return 70
    if "/docs/" in normalized or normalized.startswith("/docs/"):
        return 60
    if "/dummyserver/" in normalized or normalized.startswith("/dummyserver/"):
        return 60
    if "/src/" in normalized or normalized.startswith("/src/"):
        return 25 if leaf.startswith("_") else 20
    if leaf in {"pyproject.toml", "setup.cfg", "setup.py"}:
        return 30
    if leaf.startswith("readme"):
        return 40
    if PurePosixPath(path).suffix.casefold() == ".py":
        return 25
    return 50


def _diff_selection_key(path: str) -> tuple[int, str]:
    return (_diff_selection_priority(path), path)


def _is_release_document(path: str) -> bool:
    return (
        PurePosixPath(path)
        .name.casefold()
        .startswith(("readme", "changelog", "changes", "history"))
    )


def _safe_path(raw: str) -> str:
    normalized = posixpath.normpath(raw.replace("\\", "/"))
    path = PurePosixPath(normalized)
    if path.is_absolute() or normalized in {"", ".", ".."} or ".." in path.parts:
        raise UnsafeArtifact("archive contains an unsafe path")
    return str(path)


def _is_context_file(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return (
        name in {"metadata", "pkg-info"}
        or name.startswith(("readme", "changelog", "changes", "history"))
        or PurePosixPath(path).suffix.lower()
        in {".py", ".toml", ".cfg", ".ini", ".md", ".rst", ".txt"}
    )


def _document_changes(before: _ComparisonView, after: _ComparisonView) -> list[Json]:
    names = sorted(set(before.text_files) | set(after.text_files))
    result: list[Json] = []
    for path in names:
        if not _is_release_document(path):
            continue
        if before.text_files.get(path) != after.text_files.get(path):
            before_text = before.text_files.get(path, "")
            after_text = after.text_files.get(path, "")
            result.append(
                {
                    "evidence_id": _stable_evidence_id(
                        "document-diff",
                        before.snapshot,
                        after.snapshot,
                        path,
                        hashlib.sha256(
                            before_text.encode("utf-8") + b"\0" + after_text.encode("utf-8")
                        ).hexdigest(),
                    ),
                    "kind": "document_diff",
                    "path": path,
                    "baseline_path": before.original_paths.get(path),
                    "candidate_path": after.original_paths.get(path),
                    "baseline_sha256": before.manifest.get(path, {}).get("sha256"),
                    "candidate_sha256": after.manifest.get(path, {}).get("sha256"),
                    "before": before_text[:1_500],
                    "after": after_text[:1_500],
                    "selection_priority": 10,
                    "truncated": len(before_text) > 1_500 or len(after_text) > 1_500,
                }
            )
    return result[:10]
