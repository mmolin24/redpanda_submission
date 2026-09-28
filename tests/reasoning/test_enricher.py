from __future__ import annotations

import io
import urllib.error
import zipfile
from contextlib import contextmanager
from email.message import Message

import pytest
from helpers import event

from reasoning_worker.evidence import (
    BoundedHttpByteFetcher,
    BoundedHttpJsonFetcher,
    ExactReleaseNotFound,
    PyPIEnricher,
    PyPIResourceNotFound,
)


class Fetcher:
    def __init__(self, values):
        self.values = values
        self.urls = []

    def fetch_json(self, url):
        self.urls.append(url)
        value = self.values[url]
        if isinstance(value, BaseException):
            raise value
        return value


class Bytes:
    def __init__(self, values):
        self.values = values
        self.urls = []

    def fetch_bytes(self, url, max_bytes):
        self.urls.append(url)
        value = self.values[url]
        assert len(value) <= max_bytes
        return value


def wheel(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return buffer.getvalue()


def test_pypi_enricher_selects_baseline_by_upload_time_not_arrival_order():
    release_event = event()
    base = "https://pypi.org/pypi/dependency-b"
    values = {
        f"{base}/2.0.0/json": {
            "info": {
                "name": "dependency-b",
                "version": "2.0.0",
                "requires_python": ">=3.10",
            },
            "urls": [
                {
                    "filename": "new.whl",
                    "packagetype": "bdist_wheel",
                    "url": "https://files.pythonhosted.org/new.whl",
                    "upload_time_iso_8601": "2026-07-20T12:00:00Z",
                }
            ],
            "vulnerabilities": [],
        },
        f"{base}/json": {
            "releases": {
                "1.8.0": [{"upload_time_iso_8601": "2026-01-01T00:00:00Z", "yanked": False}],
                "1.9.0": [{"upload_time_iso_8601": "2026-07-01T00:00:00Z", "yanked": False}],
                "2.1.0": [{"upload_time_iso_8601": "2026-08-01T00:00:00Z", "yanked": False}],
                "legacy release": [
                    {"upload_time_iso_8601": "2025-01-01T00:00:00Z", "yanked": False}
                ],
            }
        },
        f"{base}/1.9.0/json": {
            "info": {
                "name": "dependency-b",
                "version": "1.9.0",
                "requires_python": ">=3.9",
            },
            "urls": [
                {
                    "filename": "old.whl",
                    "packagetype": "bdist_wheel",
                    "url": "https://files.pythonhosted.org/old.whl",
                }
            ],
            "vulnerabilities": [],
        },
    }
    fetcher = Fetcher(values)
    byte_fetcher = Bytes(
        {
            "https://files.pythonhosted.org/old.whl": wheel({"pkg/main.py": "VALUE=1\n"}),
            "https://files.pythonhosted.org/new.whl": wheel({"pkg/main.py": "VALUE=2\n"}),
        }
    )
    bundle = PyPIEnricher(fetcher, byte_fetcher=byte_fetcher).enrich(release_event)
    assert bundle.baseline["version"] == "1.9.0"
    assert bundle.collection_status == "complete"
    assert f"{base}/1.9.0/json" in fetcher.urls
    assert bundle.context["artifact_manifest_diff"]["changed"] == ["pkg/main.py"]
    assert len(bundle.provenance) == 5


def test_artifact_fetcher_rejects_disallowed_redirect_target(monkeypatch):
    class Response:
        def geturl(self):
            return "https://attacker.example/pkg.whl"

        def read(self, _size):
            raise AssertionError("redirect must be rejected before reading")

    @contextmanager
    def open_response(*_args, **_kwargs):
        yield Response()

    monkeypatch.setattr("urllib.request.urlopen", open_response)
    with pytest.raises(ValueError, match="redirect target"):
        BoundedHttpByteFetcher().fetch_bytes("https://files.pythonhosted.org/pkg.whl", 100)


def test_json_fetcher_rejects_disallowed_source_before_network(monkeypatch):
    def unexpected_open(*_args, **_kwargs):
        raise AssertionError("disallowed source must be rejected before network access")

    monkeypatch.setattr("urllib.request.urlopen", unexpected_open)
    with pytest.raises(ValueError, match="outside the PyPI allowlist"):
        BoundedHttpJsonFetcher().fetch_json("https://attacker.example/project.json")


def test_json_fetcher_rejects_disallowed_redirect_before_reading(monkeypatch):
    class Response:
        def geturl(self):
            return "https://attacker.example/project.json"

        def read(self, _size):
            raise AssertionError("redirect must be rejected before reading")

    @contextmanager
    def open_response(*_args, **_kwargs):
        yield Response()

    monkeypatch.setattr("urllib.request.urlopen", open_response)
    with pytest.raises(ValueError, match="redirect target"):
        BoundedHttpJsonFetcher().fetch_json("https://pypi.org/pypi/project/json")


def test_json_fetcher_classifies_only_http_404_as_missing_pypi_resource(
    monkeypatch,
):
    url = "https://pypi.org/pypi/dependency-b/2.0.0/json"

    def missing(*_args, **_kwargs):
        raise urllib.error.HTTPError(url, 404, "not found", Message(), None)

    monkeypatch.setattr("urllib.request.urlopen", missing)
    with pytest.raises(PyPIResourceNotFound):
        BoundedHttpJsonFetcher().fetch_json(url)


def test_only_missing_candidate_metadata_is_an_exact_release_failure():
    release_event = event()
    base = "https://pypi.org/pypi/dependency-b"

    with pytest.raises(ExactReleaseNotFound):
        PyPIEnricher(
            Fetcher({f"{base}/2.0.0/json": PyPIResourceNotFound("candidate missing")})
        ).enrich(release_event)

    with pytest.raises(FileNotFoundError) as local_error:
        PyPIEnricher(
            Fetcher({f"{base}/2.0.0/json": FileNotFoundError("local trust store missing")})
        ).enrich(release_event)
    assert not isinstance(local_error.value, ExactReleaseNotFound)

    fetcher = Fetcher(
        {
            f"{base}/2.0.0/json": {
                "info": {"name": "dependency-b", "version": "2.0.0"},
                "urls": [],
                "vulnerabilities": [],
            },
            f"{base}/json": PyPIResourceNotFound("project index unavailable"),
        }
    )
    with pytest.raises(FileNotFoundError) as error:
        PyPIEnricher(fetcher).enrich(release_event)
    assert not isinstance(error.value, ExactReleaseNotFound)


def test_missing_project_release_history_keeps_evidence_partial():
    release_event = event()
    base = "https://pypi.org/pypi/dependency-b"
    candidate = {
        "info": {"name": "dependency-b", "version": "2.0.0"},
        "urls": [],
        "vulnerabilities": [],
    }
    bundle = PyPIEnricher(
        Fetcher(
            {
                f"{base}/2.0.0/json": candidate,
                f"{base}/json": {"info": {"name": "dependency-b"}},
            }
        )
    ).enrich(release_event)

    assert bundle.baseline["version"] is None
    assert bundle.collection_status == "partial"


def test_missing_required_release_urls_fails_at_the_response_boundary():
    release_event = event()
    base = "https://pypi.org/pypi/dependency-b"
    with pytest.raises(KeyError, match="urls"):
        PyPIEnricher(
            Fetcher(
                {
                    f"{base}/2.0.0/json": {
                        "info": {"name": "dependency-b", "version": "2.0.0"},
                        "vulnerabilities": [],
                    },
                    f"{base}/json": {"releases": {}},
                }
            )
        ).enrich(release_event)
