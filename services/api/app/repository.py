"""Build read models from persisted release-analysis data."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import quote

import asyncpg

from .config import SourceMode
from .database import DatabaseUnavailableError

_LIVE_RECENCY_SECONDS = 180


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _list(value: Any) -> list[Any]:
    parsed = _json(value, [])
    return parsed if isinstance(parsed, list) else []


def _dict(value: Any) -> dict[str, Any]:
    parsed = _json(value, {})
    return parsed if isinstance(parsed, dict) else {}


_MODEL_CALL_SUMMARY_FIELDS = {
    "model_call_id",
    "processing_attempt_id",
    "logical_purpose",
    "physical_attempt",
    "analysis_trace_id",
    "span_id",
    "client_request_id",
    "openai_request_id",
    "response_id",
    "requested_model",
    "returned_model",
    "requested_service_tier",
    "returned_service_tier",
    "reasoning_effort",
    "outcome",
    "request_sha256",
    "response_sha256",
    "prompt_sha256",
    "schema_sha256",
    "evidence_sha256",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "estimated_cost_usd",
    "cost_table_version",
    "client_duration_ms",
    "openai_processing_ms",
    "started_at",
    "completed_at",
}


def _safe_model_calls(value: Any) -> list[dict[str, Any]]:
    return [
        {key: item[key] for key in _MODEL_CALL_SUMMARY_FIELDS if key in item}
        for item in _list(value)
        if isinstance(item, dict)
    ]


def _confidence(run: dict[str, Any]) -> float | None:
    applicability = run.get("applicability_confidence")
    return applicability if applicability is not None else run.get("materiality_confidence")


def _grafana_time_range(
    started_at: datetime | None,
    completed_at: datetime | None,
) -> dict[str, str]:
    """Bound Explore to the persisted processing attempt, with useful log context."""
    if not started_at or not completed_at:
        return {"from": "now-6h", "to": "now"}
    start = int((started_at - timedelta(minutes=5)).timestamp() * 1000)
    end = int((completed_at + timedelta(minutes=5)).timestamp() * 1000)
    return {"from": str(start), "to": str(end)}


def _grafana_urls(
    base_url: str,
    trace_id: str,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> dict[str, str]:
    """Build Grafana 13-compatible deep links using provisioned datasource UIDs."""
    time_range = _grafana_time_range(started_at, completed_at)
    trace_pane = {
        "range": time_range,
        "datasource": "tempo",
        "queries": [
            {
                "refId": "A",
                "query": trace_id,
                "queryType": "traceql",
                "datasource": {"type": "tempo", "uid": "tempo"},
                "limit": 20,
                "tableType": "traces",
            }
        ],
        "compact": False,
    }
    logs_pane = {
        "range": time_range,
        "datasource": "loki",
        "queries": [
            {
                "refId": "A",
                "expr": (
                    '{deployment_environment="local",service=~".*reasoning-worker.*"} '
                    f'| json | trace_id = "{trace_id}"'
                ),
                "datasource": {"type": "loki", "uid": "loki"},
                "editorMode": "code",
                "queryType": "range",
                "direction": "backward",
            }
        ],
        "panelsState": {"logs": {"sortOrder": "Descending"}},
        "compact": False,
    }

    def explore(pane_id: str, pane: dict[str, Any]) -> str:
        encoded_state = quote(
            json.dumps({pane_id: pane}, separators=(",", ":")),
            safe="",
        )
        return f"{base_url.rstrip('/')}/explore?schemaVersion=1&panes={encoded_state}&orgId=1"

    encoded_trace_id = quote(trace_id, safe="")
    return {
        "trace": explore("trace", trace_pane),
        "logs": explore("logs", logs_pane),
        "model_payloads": (
            f"{base_url.rstrip('/')}/d/pypi-reasoning/pypi-reasoning-and-openai"
            f"?orgId=1&var-trace_id={encoded_trace_id}&viewPanel=6"
        ),
    }


def _data_freshness(
    source_mode: SourceMode,
    last_ingestion: datetime | None,
    observed_at: datetime,
) -> dict[str, Any]:
    if last_ingestion is None:
        return {
            "source_mode": source_mode.value,
            "status": "waiting",
            "last_ingestion_at": None,
            "age_seconds": None,
            "detail": "No release data has been persisted yet.",
        }

    age_seconds = max(0.0, (observed_at - last_ingestion).total_seconds())
    if source_mode is SourceMode.FIXTURE:
        status = "fixture"
        detail = "Static fixture data is available; its age is not pipeline health."
    elif source_mode is SourceMode.HISTORY:
        status = "historical"
        detail = "Historical release data is available; its age is not pipeline health."
    elif age_seconds <= _LIVE_RECENCY_SECONDS:
        status = "recent"
        detail = "A live-feed release event was persisted recently."
    else:
        status = "quiet"
        detail = (
            "No live-feed release event was persisted recently; "
            "this alone does not determine pipeline health."
        )

    return {
        "source_mode": source_mode.value,
        "status": status,
        "last_ingestion_at": last_ingestion,
        "age_seconds": age_seconds,
        "detail": detail,
    }


def _unavailable_ops_summary(source_mode: SourceMode) -> dict[str, Any]:
    return {
        "status": "degraded",
        "freshness": {
            "source_mode": source_mode.value,
            "status": "unknown",
            "last_ingestion_at": None,
            "age_seconds": None,
            "detail": "Data recency is unavailable while the database cannot be read.",
        },
        "attention": {
            "status": "unknown",
            "unresolved_failures": None,
            "detail": (
                "Processing-failure attention is unavailable while the database cannot be read."
            ),
        },
        "components": {
            "postgres": {
                "status": "degraded",
                "detail": "database unavailable",
            },
            "telemetry": {
                "status": "unknown",
                "detail": "consult provisioned Grafana dashboards",
            },
        },
        "recent_dispositions": {},
        "unresolved_failures": None,
        "openai": {
            "estimated_cost_usd": None,
            "price_table_date": None,
        },
    }


class ReadRepository(Protocol):
    """Define the read operations consumed by API routes."""

    async def ready(self) -> bool: ...

    async def list_findings(self, **filters: Any) -> dict[str, Any]: ...

    async def get_finding(self, finding_id: str) -> dict[str, Any] | None: ...

    async def get_package(self, normalized_name: str) -> dict[str, Any] | None: ...

    async def get_stats(self) -> dict[str, Any]: ...

    async def get_ops_summary(self) -> dict[str, Any]: ...

    async def get_trace_summary(self, finding_id: str) -> dict[str, Any] | None: ...


class ReadPool(Protocol):
    """Define the asyncpg query surface needed by the repository."""

    async def fetchval(self, query: str, /, *args: object) -> Any: ...

    async def fetch(
        self,
        query: str,
        /,
        *args: object,
    ) -> Sequence[Mapping[str, Any]]: ...

    async def fetchrow(
        self,
        query: str,
        /,
        *args: object,
    ) -> Mapping[str, Any] | None: ...


class ReadDatabase(Protocol):
    """Provide readiness and a query pool to the read repository."""

    @property
    def pool(self) -> ReadPool: ...

    async def ready(self) -> bool: ...


class PostgresReadRepository:
    """Build customer and operator read models from normalized Postgres data."""

    def __init__(
        self,
        database: ReadDatabase,
        grafana_base_url: str,
        source_mode: SourceMode = SourceMode.FIXTURE,
    ) -> None:
        self._db = database
        self._grafana = grafana_base_url.rstrip("/")
        self._source_mode = source_mode

    async def ready(self) -> bool:
        return await self._db.ready()

    @staticmethod
    def _summary(row: Any) -> dict[str, Any]:
        data = dict(row)
        run = _dict(data.pop("run_data", {}))
        evidence = _dict(data.pop("evidence_bundle", {}))
        observability = _dict(run.get("observability"))
        data["confidence"] = _confidence(run)
        data["processing_priority"] = run.get("processing_priority")
        data["disposition"] = run.get("disposition", "publishable")
        data["publishable"] = bool(run.get("publishable", False))
        data["analysis_method"] = run.get("analysis_method", "model_assisted")
        data["analysis_version"] = run.get("analysis_version")
        data["analysis_trace_id"] = run.get("analysis_trace_id") or observability.get(
            "analysis_trace_id"
        )
        data["evidence_partial"] = evidence.get("collection_status") in {
            "partial",
            "failed",
            "insufficient",
        }
        return data

    async def list_findings(self, **filters: Any) -> dict[str, Any]:
        clauses = [
            """ar.finding_id = (
                select latest_attempt.finding_id
                from analysis_attempts latest_attempt
                where latest_attempt.event_key = ar.event_key
                  and latest_attempt.finding_id is not null
                order by latest_attempt.completed_at desc,
                         latest_attempt.processing_attempt_id desc
                limit 1
            )""",
        ]
        args: list[Any] = [filters.get("publishable", True)]
        if filters.get("include_insufficient", False) and args[0] is True:
            clauses.insert(
                0,
                "(ar.publishable = $1 OR ar.disposition = 'insufficient_evidence')",
            )
        else:
            clauses.insert(0, "ar.publishable = $1")

        def add(clause: str, value: Any) -> None:
            args.append(value)
            clauses.append(clause.replace("?", f"${len(args)}"))

        if value := filters.get("package"):
            add("f.package_name ilike ?", f"%{value}%")
        if value := filters.get("change_type"):
            add("? = any(f.change_types)", value)
        if value := filters.get("processing_priority"):
            add("ar.processing_priority = ?", value)
        if (value := filters.get("min_confidence")) is not None:
            add(
                "coalesce(ar.applicability_confidence, ar.materiality_confidence, 0) >= ?",
                value,
            )
        if value := filters.get("disposition"):
            add("ar.disposition = ?", value)
        if value := filters.get("published_from"):
            add("f.published_at >= ?", value)
        if value := filters.get("published_to"):
            add("f.published_at <= ?", value)

        where = " and ".join(clauses)
        # Query fragments come only from the closed maps and fixed clauses
        # above. Every request-controlled value remains an asyncpg bind arg.
        count_query = f"""
            select count(*)
            from findings f
            join analysis_runs ar using (finding_id)
            where {where}
            """  # noqa: S608
        total = await self._db.pool.fetchval(
            count_query,
            *args,
        )
        order_column = "re.published_at"
        order_direction = "asc" if filters.get("sort_direction") == "asc" else "desc"
        page = filters["page"]
        page_size = filters["page_size"]
        args.extend([page_size, (page - 1) * page_size])
        page_query = f"""
            select f.finding_id, f.package_name, f.baseline_version, f.candidate_version,
                   f.change_types, f.assessment, f.published_at,
                   re.published_at as event_published_at, re.ingested_at,
                   f.evidence_bundle, to_jsonb(ar) as run_data
            from findings f
            join analysis_runs ar using (finding_id)
            join release_events re on re.event_key = ar.event_key
            where {where}
            order by {order_column} {order_direction} nulls last,
                     f.published_at desc nulls last
            limit ${len(args) - 1} offset ${len(args)}
            """  # noqa: S608
        rows = await self._db.pool.fetch(
            page_query,
            *args,
        )
        return {
            "items": [self._summary(row) for row in rows],
            "meta": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "pages": math.ceil(total / page_size) if total else 0,
            },
        }

    async def get_finding(self, finding_id: str) -> dict[str, Any] | None:
        row = await self._db.pool.fetchrow(
            """
            select to_jsonb(f) as finding_data, to_jsonb(ar) as run_data,
                   re.raw_event as event_data, re.published_at as event_published_at,
                   re.ingested_at
            from findings f
            join analysis_runs ar using (finding_id)
            join release_events re on re.event_key = ar.event_key
            where f.finding_id = $1
            """,
            finding_id,
        )
        if row is None:
            return None
        row_data = dict(row)
        finding = _dict(row_data["finding_data"])
        run = _dict(row_data["run_data"])
        observability = _dict(run.get("observability"))
        finding.update(
            analysis_version=run.get("analysis_version"),
            confidence=_confidence(run),
            processing_priority=run.get("processing_priority"),
            disposition=run.get("disposition", "publishable"),
            publishable=bool(run.get("publishable", False)),
            analysis_method=run.get("analysis_method", "model_assisted"),
            gate_results=_dict(run.get("gate_results")),
            model_calls=_safe_model_calls(run.get("model_calls")),
            source_event=_dict(row_data["event_data"]),
            event_published_at=row_data.get("event_published_at"),
            ingested_at=row_data.get("ingested_at"),
            observability=observability
            | {
                "analysis_trace_id": run.get("analysis_trace_id")
                or observability.get("analysis_trace_id")
            },
        )
        return finding

    async def get_package(self, normalized_name: str) -> dict[str, Any] | None:
        releases = await self._db.pool.fetch(
            """
            select to_jsonb(re) as data from release_events re
            where re.package_name = $1 order by re.published_at desc nulls last
            """,
            normalized_name,
        )
        findings = await self._db.pool.fetch(
            """
            select f.finding_id, f.package_name, f.baseline_version, f.candidate_version,
                   f.change_types, f.assessment, f.published_at,
                   re.published_at as event_published_at, re.ingested_at,
                   f.evidence_bundle, to_jsonb(ar) as run_data
            from findings f
            join analysis_runs ar using (finding_id)
            join release_events re on re.event_key = ar.event_key
            where f.package_name = $1 order by re.published_at desc nulls last
            """,
            normalized_name,
        )
        if not releases and not findings:
            return None
        return {
            "normalized_name": normalized_name,
            "findings": [self._summary(row) for row in findings],
            "releases": [_dict(row["data"]) for row in releases],
        }

    async def get_stats(self) -> dict[str, Any]:
        disposition_rows = await self._db.pool.fetch(
            "select disposition, count(*) as count from analysis_runs group by disposition"
        )
        materiality_rows = await self._db.pool.fetch(
            "select coalesce(materiality_decision, 'not_run') as materiality, count(*) as count "
            "from analysis_runs group by materiality_decision"
        )
        failure_stats = await self._db.pool.fetchrow(
            """
            select (select count(*) from processing_failures where resolved_at is null)
                       as unresolved_failures,
                   (select count(*) from release_events) as release_event_count,
                   (select max(ingested_at) from release_events) as last_ingestion,
                   (
                     select jsonb_build_object(
                       'event_key', re.event_key,
                       'package_name', re.package_name,
                       'version', re.version,
                       'event_published_at', re.published_at,
                       'ingested_at', re.ingested_at,
                       'disposition', latest_analysis.disposition,
                       'analysis_method', latest_analysis.analysis_method,
                       'publishable', latest_analysis.publishable
                     )
                     from release_events re
                     left join lateral (
                       select ar.disposition, ar.analysis_method, ar.publishable
                       from analysis_runs ar
                       where ar.event_key = re.event_key
                       order by ar.created_at desc, ar.finding_id desc
                       limit 1
                     ) latest_analysis on true
                     order by re.published_at desc nulls last,
                              re.ingested_at desc,
                              re.event_key desc
                     limit 1
                   ) as latest_release
            """
        )
        cost = await self._db.pool.fetchrow(
            """
            select coalesce(sum(estimated_cost_usd), 0) as total,
                   (array_agg(cost_table_version order by completed_at desc)
                    filter (where cost_table_version is not null))[1] as version
            from model_calls
            """
        )
        if failure_stats is None or cost is None:
            raise DatabaseUnavailableError("database aggregate query returned no row")
        return {
            "materiality_counts": {row["materiality"]: row["count"] for row in materiality_rows},
            "disposition_counts": {row["disposition"]: row["count"] for row in disposition_rows},
            "release_event_count": failure_stats["release_event_count"],
            "latest_release": _dict(failure_stats["latest_release"])
            if failure_stats["latest_release"] is not None
            else None,
            "unresolved_failures": failure_stats["unresolved_failures"],
            "last_ingestion_at": failure_stats["last_ingestion"],
            "estimated_cost_usd": float(cost["total"]),
            "price_table_date": cost["version"],
        }

    async def get_ops_summary(self) -> dict[str, Any]:
        ready = await self.ready()
        if not ready:
            return _unavailable_ops_summary(self._source_mode)
        try:
            stats = await self.get_stats()
        except (asyncpg.PostgresError, DatabaseUnavailableError, OSError):
            return _unavailable_ops_summary(self._source_mode)

        last_ingestion = stats["last_ingestion_at"]
        unresolved_failures = int(stats["unresolved_failures"])
        record_phrase = "record is" if unresolved_failures == 1 else "records are"
        return {
            "status": "healthy",
            "freshness": _data_freshness(
                self._source_mode,
                last_ingestion,
                datetime.now(UTC),
            ),
            "attention": {
                "status": "attention" if unresolved_failures else "clear",
                "unresolved_failures": unresolved_failures,
                "detail": (
                    f"{unresolved_failures} failed processing {record_phrase} "
                    "safely retained for review; this count does not indicate "
                    "infrastructure backpressure."
                    if unresolved_failures
                    else "No unresolved processing failures."
                ),
            },
            "components": {
                "postgres": {
                    "status": "healthy",
                    "detail": "read queries ready",
                },
                "telemetry": {
                    "status": "unknown",
                    "detail": "consult provisioned Grafana dashboards",
                },
            },
            "recent_dispositions": stats["disposition_counts"],
            "unresolved_failures": unresolved_failures,
            "openai": {
                "estimated_cost_usd": stats["estimated_cost_usd"],
                "price_table_date": stats["price_table_date"],
            },
        }

    async def get_trace_summary(self, finding_id: str) -> dict[str, Any] | None:
        run_row = await self._db.pool.fetchrow(
            """
            select to_jsonb(ar) as run_data from analysis_runs ar where finding_id = $1
            """,
            finding_id,
        )
        if run_row is None:
            return None
        run = _dict(run_row["run_data"])
        observability = _dict(run.get("observability"))
        attempt = await self._db.pool.fetchrow(
            """
            select processing_attempt_id, stage_summary, started_at, completed_at
            from analysis_attempts where finding_id = $1
            order by completed_at desc limit 1
            """,
            finding_id,
        )
        stages = _list(
            (attempt["stage_summary"] if attempt else None)
            or observability.get("stage_summary")
            or run.get("stage_summary")
        )
        trace_id = run.get("analysis_trace_id") or observability.get("analysis_trace_id")
        processing_attempt_id = (
            attempt["processing_attempt_id"]
            if attempt
            else observability.get("processing_attempt_id")
        )
        if not trace_id or not processing_attempt_id:
            return None
        calls = await self._db.pool.fetch(
            """
            select to_jsonb(mc) - 'request_payload' - 'response_payload' as data
            from model_calls mc
            where mc.processing_attempt_id = $1
            order by mc.started_at, mc.physical_attempt
            """,
            processing_attempt_id,
        )
        return {
            "finding_id": finding_id,
            "analysis_trace_id": trace_id,
            "processing_attempt_id": processing_attempt_id,
            "stage_summary": stages,
            "model_calls": [_dict(row["data"]) for row in calls],
            "grafana_urls": _grafana_urls(
                self._grafana,
                str(trace_id),
                attempt["started_at"] if attempt else None,
                attempt["completed_at"] if attempt else None,
            ),
        }
