from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, SourceMode
from app.database import DatabaseUnavailableError
from app.main import create_app
from app.repository import PostgresReadRepository, _data_freshness, _grafana_urls

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


class ReadPoolStub:
    async def fetchval(self, _query: str, *_args: object) -> Any:
        raise AssertionError("unexpected fetchval")

    async def fetch(self, _query: str, *_args: object) -> list[dict[str, Any]]:
        raise AssertionError("unexpected fetch")

    async def fetchrow(self, _query: str, *_args: object) -> dict[str, Any] | None:
        raise AssertionError("unexpected fetchrow")


class FakeRepository:
    ready_value = True
    finding: ClassVar[dict[str, Any]] = {
        "finding_id": "finding-1",
        "analysis_version": "analysis-v1",
        "package_name": "requests",
        "baseline_version": "2.31.0",
        "candidate_version": "2.32.0",
        "change_types": ["dependency_constraint"],
        "assessment": "A tighter dependency constraint can affect compatible environments.",
        "confidence": 0.86,
        "processing_priority": "high",
        "disposition": "publishable",
        "publishable": True,
        "analysis_method": "model_assisted",
        "event_published_at": NOW,
        "ingested_at": NOW,
        "published_at": NOW,
        "evidence_partial": False,
        "analysis_trace_id": "a" * 32,
        "evidence_bundle": {"items": [{"evidence_id": "dep-1"}]},
        "limitations": ["Configured environments only"],
        "gate_results": {"materiality": {"decision": "substantive"}},
        "model_calls": [],
        "source_event": {"event_key": "requests:2.32.0"},
        "observability": {"analysis_trace_id": "a" * 32},
    }

    async def ready(self) -> bool:
        return self.ready_value

    async def list_findings(self, **filters: Any) -> dict[str, Any]:
        return {
            "items": [self.finding],
            "meta": {
                "page": filters["page"],
                "page_size": filters["page_size"],
                "total": 1,
                "pages": 1,
            },
        }

    async def get_finding(self, finding_id: str):
        return self.finding if finding_id == "finding-1" else None

    async def get_package(self, normalized_name: str):
        if normalized_name != "requests":
            return None
        return {
            "normalized_name": normalized_name,
            "findings": [self.finding],
            "releases": [],
        }

    async def get_stats(self):
        return {
            "materiality_counts": {"substantive": 1},
            "disposition_counts": {"publishable": 1},
            "release_event_count": 1,
            "latest_release": {
                "event_key": "pypi:requests:2.32.0",
                "package_name": "requests",
                "version": "2.32.0",
                "event_published_at": NOW,
                "ingested_at": NOW,
                "disposition": "publishable",
                "analysis_method": "model_assisted",
                "publishable": True,
            },
            "unresolved_failures": 0,
            "last_ingestion_at": NOW,
            "estimated_cost_usd": 0.002,
            "price_table_date": "2026-07-20",
        }

    async def get_ops_summary(self):
        return {
            "status": "healthy",
            "freshness": {
                "source_mode": "fixture",
                "status": "fixture",
                "last_ingestion_at": NOW,
                "age_seconds": 0,
                "detail": "Static fixture data is available.",
            },
            "attention": {
                "status": "clear",
                "unresolved_failures": 0,
                "detail": "No unresolved processing failures.",
            },
            "components": {"postgres": {"status": "healthy"}},
            "recent_dispositions": {"publishable": 1},
            "unresolved_failures": 0,
            "openai": {"estimated_cost_usd": 0.002},
        }

    async def get_trace_summary(self, finding_id: str):
        if finding_id != "finding-1":
            return None
        return {
            "finding_id": finding_id,
            "analysis_trace_id": "a" * 32,
            "processing_attempt_id": "attempt-1",
            "stage_summary": [
                {"stage": "connect_source", "outcome": "completed", "duration_ms": 5}
            ],
            "model_calls": [],
            "grafana_urls": {
                "trace": "http://localhost:3000/explore?traceId=aaa",
                "logs": "http://localhost:3000/explore?var-trace_id=aaa",
                "model_payloads": "http://localhost:3000/d/model-calls?var-trace_id=aaa",
            },
        }


def client(repo: FakeRepository | None = None) -> TestClient:
    app = create_app(repo or FakeRepository(), Settings("unused", "http://localhost:3000"))
    return TestClient(app)


def test_health_reflects_repository_readiness() -> None:
    repo = FakeRepository()
    with client(repo) as http:
        assert http.get("/api/health").json() == {
            "status": "healthy",
            "database": "ready",
        }
        repo.ready_value = False
        response = http.get("/api/health")
        assert response.status_code == 503
        assert response.json()["database"] == "unavailable"


def test_findings_are_typed_and_paginated() -> None:
    with client() as http:
        response = http.get("/api/findings", params={"package": "requests", "min_confidence": 0.65})
    assert response.status_code == 200
    assert response.json()["items"][0]["analysis_trace_id"] == "a" * 32
    assert response.json()["items"][0]["analysis_version"] == "analysis-v1"
    assert response.json()["items"][0]["publishable"] is True
    assert response.json()["items"][0]["event_published_at"] == "2026-07-21T12:00:00Z"
    assert response.json()["meta"]["total"] == 1


def test_detail_package_stats_and_ops_contracts() -> None:
    with client() as http:
        assert (
            http.get("/api/findings/finding-1").json()["evidence_bundle"]["items"][0]["evidence_id"]
            == "dep-1"
        )
        assert http.get("/api/packages/requests").json()["normalized_name"] == "requests"
        assert http.get("/api/stats").json()["estimated_cost_usd"] == 0.002
        assert http.get("/api/ops/summary").json()["status"] == "healthy"


def test_settings_allowlist_source_modes_and_reject_unknown_configs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GRAFANA_BASE_URL", raising=False)
    assert Settings.from_env().grafana_base_url == "http://localhost:3001"
    monkeypatch.setenv("GRAFANA_BASE_URL", "https://observability.example/grafana")
    assert Settings.from_env().grafana_base_url == "https://observability.example/grafana"
    assert (
        Settings("unused", "http://localhost", "source-fixture.yaml").source_mode
        is SourceMode.FIXTURE
    )
    assert (
        Settings("unused", "http://localhost", "source-history.yaml").source_mode
        is SourceMode.HISTORY
    )
    assert Settings("unused", "http://localhost", "source-live.yaml").source_mode is SourceMode.LIVE

    with pytest.raises(ValueError, match="SOURCE_CONFIG"):
        Settings("unused", "http://localhost", "../surprising-source.yaml")


def test_data_freshness_describes_recency_without_claiming_delivery_health() -> None:
    old_ingestion = NOW - timedelta(days=7)

    assert _data_freshness(SourceMode.FIXTURE, old_ingestion, NOW)["status"] == "fixture"
    assert _data_freshness(SourceMode.HISTORY, old_ingestion, NOW)["status"] == "historical"
    assert _data_freshness(SourceMode.LIVE, NOW - timedelta(seconds=60), NOW)["status"] == "recent"
    quiet_live = _data_freshness(SourceMode.LIVE, old_ingestion, NOW)
    assert quiet_live["status"] == "quiet"
    assert "pipeline health" in quiet_live["detail"].lower()
    assert _data_freshness(SourceMode.LIVE, None, NOW)["status"] == "waiting"


def test_trace_summary_is_persisted_data_movement_only() -> None:
    with client() as http:
        body = http.get("/api/findings/finding-1/trace-summary").json()
    assert body["stage_summary"][0]["stage"] == "connect_source"
    assert "presentation_trace_id" not in body


def test_grafana_links_open_real_tempo_and_loki_queries() -> None:
    trace_id = "a" * 32
    links = _grafana_urls(
        "https://observability.example/grafana/",
        trace_id,
        datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
        datetime(2026, 7, 21, 12, 1, tzinfo=UTC),
    )

    assert all(url.startswith("https://observability.example/grafana/") for url in links.values())

    trace_query = parse_qs(urlparse(links["trace"]).query)
    assert trace_query["schemaVersion"] == ["1"]
    trace_state = json.loads(trace_query["panes"][0])["trace"]
    assert trace_state["datasource"] == "tempo"
    assert trace_state["queries"][0]["query"] == trace_id
    assert trace_state["queries"][0]["queryType"] == "traceql"
    assert trace_state["queries"][0]["datasource"] == {"type": "tempo", "uid": "tempo"}
    assert trace_state["range"] == {
        "from": "1784634900000",
        "to": "1784635560000",
    }

    logs_query = parse_qs(urlparse(links["logs"]).query)
    assert logs_query["schemaVersion"] == ["1"]
    logs_state = json.loads(logs_query["panes"][0])["logs"]
    assert logs_state["datasource"] == "loki"
    assert logs_state["queries"][0]["datasource"] == {"type": "loki", "uid": "loki"}
    assert "| json | trace_id = " in logs_state["queries"][0]["expr"]
    assert "reasoning-worker" in logs_state["queries"][0]["expr"]
    assert trace_id in logs_state["queries"][0]["expr"]
    assert "/d/pypi-reasoning/" in links["model_payloads"]


def test_missing_resources_are_404_and_filters_validate() -> None:
    with client() as http:
        assert http.get("/api/findings/missing").status_code == 404
        assert http.get("/api/packages/missing").status_code == 404
        assert http.get("/api/findings", params={"min_confidence": 2}).status_code == 422
        assert http.get("/api/findings", params={"sort_by": "unknown"}).status_code == 422


def test_summary_preserves_zero_confidence_and_top_level_collection_status() -> None:
    summary = PostgresReadRepository._summary(
        {
            "finding_id": "finding-1",
            "evidence_bundle": {"collection_status": "partial"},
            "run_data": {
                "materiality_confidence": 0.8,
                "applicability_confidence": 0.0,
                "disposition": "publishable",
                "publishable": True,
            },
        }
    )
    assert summary["confidence"] == 0.0
    assert summary["evidence_partial"] is True
    assert summary["publishable"] is True


def test_repository_sorts_by_source_release_event_time() -> None:
    class Pool(ReadPoolStub):
        fetch_query = ""

        async def fetchval(self, _query: str, *_args):
            return 0

        async def fetch(self, query: str, *_args):
            self.fetch_query = query
            return []

    class DB:
        pool = Pool()

        async def ready(self) -> bool:
            return True

    repository = PostgresReadRepository(DB(), "http://localhost:3000")
    asyncio.run(
        repository.list_findings(
            page=1,
            page_size=25,
            publishable=True,
            sort_by="event_time",
            sort_direction="desc",
        )
    )
    assert "join release_events re on re.event_key = ar.event_key" in DB.pool.fetch_query
    assert "order by re.published_at desc nulls last" in DB.pool.fetch_query
    assert "latest_attempt.event_key = ar.event_key" in DB.pool.fetch_query
    assert "latest_attempt.finding_id is not null" in DB.pool.fetch_query
    assert "order by latest_attempt.completed_at desc" in DB.pool.fetch_query


def test_repository_can_include_only_insufficient_evidence_with_publishable_findings() -> None:
    class Pool(ReadPoolStub):
        fetch_query = ""
        fetch_args = ()

        async def fetchval(self, query: str, *args):
            self.fetch_query = query
            self.fetch_args = args
            return 0

        async def fetch(self, query: str, *args):
            self.fetch_query = query
            self.fetch_args = args
            return []

    class DB:
        pool = Pool()

        async def ready(self) -> bool:
            return True

    repository = PostgresReadRepository(DB(), "http://localhost:3000")
    asyncio.run(
        repository.list_findings(
            page=1,
            page_size=25,
            publishable=True,
            include_insufficient=True,
            sort_by="event_time",
            sort_direction="desc",
        )
    )
    assert "ar.publishable = $1 OR ar.disposition = 'insufficient_evidence'" in DB.pool.fetch_query
    assert "suppressed_validation_failure" not in DB.pool.fetch_query
    assert DB.pool.fetch_args[:1] == (True,)


def test_trace_summary_limits_model_calls_to_latest_processing_attempt() -> None:
    class Pool(ReadPoolStub):
        fetch_query = ""
        fetch_args = ()
        fetchrow_count = 0

        async def fetchrow(self, _query: str, *_args):
            self.fetchrow_count += 1
            if self.fetchrow_count == 1:
                return {"run_data": {"analysis_trace_id": "a" * 32}}
            return {
                "processing_attempt_id": "attempt-latest",
                "stage_summary": [],
                "started_at": NOW,
                "completed_at": NOW,
            }

        async def fetch(self, query: str, *args):
            self.fetch_query = query
            self.fetch_args = args
            return []

    class DB:
        pool = Pool()

        async def ready(self) -> bool:
            return True

    repository = PostgresReadRepository(DB(), "http://localhost:3000")
    summary = asyncio.run(repository.get_trace_summary("finding-1"))

    assert summary is not None
    assert "where mc.processing_attempt_id = $1" in DB.pool.fetch_query
    assert DB.pool.fetch_args == ("attempt-latest",)


def test_repository_returns_raw_source_event() -> None:
    raw_event = {
        "schema_version": "release-event.v1",
        "event_key": "pypi:requests:2.32.0",
    }

    class Pool(ReadPoolStub):
        async def fetchrow(self, query: str, *_args):
            if "from findings f" in query:
                return {
                    "finding_data": FakeRepository.finding,
                    "run_data": {
                        "materiality_confidence": 0.8,
                        "applicability_confidence": 0.0,
                        "disposition": "publishable",
                        "gate_results": {},
                        "model_calls": [],
                    },
                    "event_data": raw_event,
                    "event_published_at": NOW,
                    "ingested_at": NOW,
                }
            return None

    class DB:
        pool = Pool()

        async def ready(self) -> bool:
            return True

    repository = PostgresReadRepository(DB(), "http://localhost:3000")
    detail = asyncio.run(repository.get_finding("finding-1"))
    assert detail is not None
    assert detail["source_event"] == raw_event
    assert detail["event_published_at"] == NOW
    assert detail["confidence"] == 0.0


def test_repository_stats_uses_named_aggregate_fields() -> None:
    class Pool(ReadPoolStub):
        async def fetch(self, query: str, *_args: object) -> list[dict[str, Any]]:
            if "group by disposition" in query:
                return [{"disposition": "publishable", "count": 3}]
            return [{"materiality": "substantive", "count": 2}]

        async def fetchrow(self, query: str, *_args: object) -> dict[str, Any]:
            if "unresolved_failures" in query:
                return {
                    "unresolved_failures": 4,
                    "release_event_count": 1,
                    "last_ingestion": NOW,
                    "latest_release": {
                        "event_key": "pypi:requests:2.32.0",
                        "package_name": "requests",
                        "version": "2.32.0",
                        "event_published_at": NOW,
                        "ingested_at": NOW,
                        "disposition": "suppressed_validation_failure",
                        "analysis_method": "model_assisted",
                        "publishable": False,
                    },
                }
            return {"total": 0.125, "version": "price-v1"}

    class DB:
        pool = Pool()

        async def ready(self) -> bool:
            return True

    repository = PostgresReadRepository(DB(), "http://localhost:3000")
    stats = asyncio.run(repository.get_stats())

    assert stats["disposition_counts"] == {"publishable": 3}
    assert stats["materiality_counts"] == {"substantive": 2}
    assert stats["unresolved_failures"] == 4
    assert stats["release_event_count"] == 1
    assert stats["latest_release"]["version"] == "2.32.0"
    assert stats["latest_release"]["publishable"] is False
    assert stats["last_ingestion_at"] == NOW
    assert stats["estimated_cost_usd"] == 0.125
    assert stats["price_table_date"] == "price-v1"


def test_ops_summary_keeps_retained_failures_separate_from_dependency_health() -> None:
    class Pool(ReadPoolStub):
        async def fetch(self, query: str, *_args: object) -> list[dict[str, Any]]:
            if "group by disposition" in query:
                return [{"disposition": "publishable", "count": 3}]
            return [{"materiality": "substantive", "count": 2}]

        async def fetchrow(self, query: str, *_args: object) -> dict[str, Any]:
            if "unresolved_failures" in query:
                return {
                    "unresolved_failures": 4,
                    "release_event_count": 1,
                    "last_ingestion": NOW,
                    "latest_release": None,
                }
            return {"total": 0.125, "version": "price-v1"}

    class DB:
        pool = Pool()

        async def ready(self) -> bool:
            return True

    database = DB()
    repository = PostgresReadRepository(
        database,
        "http://localhost:3000",
        source_mode=SourceMode.FIXTURE,
    )
    summary = asyncio.run(repository.get_ops_summary())

    assert summary["status"] == "healthy"
    assert summary["components"]["postgres"]["status"] == "healthy"
    assert summary["attention"] == {
        "status": "attention",
        "unresolved_failures": 4,
        "detail": (
            "4 failed processing records are safely retained for review; "
            "this count does not indicate infrastructure backpressure."
        ),
    }


def test_ops_summary_does_not_query_or_invent_stats_when_database_is_unavailable() -> None:
    class FailingPool(ReadPoolStub):
        async def fetch(self, _query: str, *_args: object) -> list[dict[str, Any]]:
            raise DatabaseUnavailableError("connection was lost")

    class DB:
        def __init__(self, ready: bool, pool: ReadPoolStub) -> None:
            self.ready_value = ready
            self.pool = pool

        async def ready(self) -> bool:
            return self.ready_value

    for database in (
        DB(False, ReadPoolStub()),
        DB(True, FailingPool()),
    ):
        repository = PostgresReadRepository(
            database,
            "http://localhost:3000",
            source_mode=SourceMode.HISTORY,
        )
        summary = asyncio.run(repository.get_ops_summary())

        assert summary["status"] == "degraded"
        assert summary["freshness"] == {
            "source_mode": "history",
            "status": "unknown",
            "last_ingestion_at": None,
            "age_seconds": None,
            "detail": "Data recency is unavailable while the database cannot be read.",
        }
        assert summary["attention"] == {
            "status": "unknown",
            "unresolved_failures": None,
            "detail": (
                "Processing-failure attention is unavailable while the database cannot be read."
            ),
        }
        assert summary["unresolved_failures"] is None
        assert summary["recent_dispositions"] == {}
        assert summary["openai"] == {
            "estimated_cost_usd": None,
            "price_table_date": None,
        }
