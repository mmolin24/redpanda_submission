#!/bin/sh
set -eu

smoke_stage() {
  if [ -n "${SMOKE_PROGRESS_FILE:-}" ]; then
    printf '%s\n' "$1" >"${SMOKE_PROGRESS_FILE}"
  fi
}

if [ -z "${SMOKE_PROJECT_NAME:-}" ] \
  || [ -z "${SMOKE_COMPOSE_FILE:-}" ] \
  || [ -z "${SMOKE_COMPOSE_OVERRIDE_FILE:-}" ] \
  || [ -z "${SMOKE_COMPOSE_EXECUTABLE:-}" ] \
  || [ -z "${SMOKE_PROJECT_DIRECTORY:-}" ]; then
  echo "Smoke scope is not configured" >&2
  exit 1
fi

compose() {
  "${SMOKE_COMPOSE_EXECUTABLE}" \
    --project-name "${SMOKE_PROJECT_NAME}" \
    --env-file /dev/null \
    --file "${SMOKE_COMPOSE_FILE}" \
    --file "${SMOKE_COMPOSE_OVERRIDE_FILE}" \
    --project-directory "${SMOKE_PROJECT_DIRECTORY}" \
    "$@"
}

smoke_scenario="${SMOKE_SCENARIO:-ingestion-relevance}"
case "$smoke_scenario" in
  ingestion-relevance | exact-release-missing | duplicate-terminal-replay | analysis-version-replay | deterministic-finding-ui | end-to-end-trace | runtime-signals | full-stack-drain | worker-shutdown) ;;
  *)
    echo "Unsupported smoke scenario" >&2
    exit 1
    ;;
esac

requires_full_observability=0
case "$smoke_scenario" in
  ingestion-relevance | end-to-end-trace) requires_full_observability=1 ;;
esac

compose_url() {
  service="$1"
  container_port="$2"
  published="$(compose port "$service" "$container_port" | tail -n 1)"
  if [ -z "$published" ]; then
    echo "No published port found for ${service}:${container_port}" >&2
    return 1
  fi
  printf 'http://127.0.0.1:%s' "${published##*:}"
}

wait_url() {
  name="$1"
  url="$2"
  attempt=0
  until curl --fail --silent --max-time 5 "$url" >/dev/null; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
      echo "Timed out waiting for ${name}: ${url}" >&2
      return 1
    fi
    sleep 2
  done
}

wait_command() {
  name="$1"
  shift
  attempt=0
  until "$@" >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
      echo "Timed out waiting for ${name}" >&2
      return 1
    fi
    sleep 2
  done
}

query_db() {
  sql="$1"
  compose exec -T postgres sh -c \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "$1"' sh "$sql"
}

metric_sum() {
  metric_name="$1"
  required_label="$2"
  awk -v metric_name="$metric_name" -v required_label="$required_label" '
    index($0, metric_name "{") == 1 && index($0, required_label) > 0 { total += $NF }
    END { print total + 0 }
  '
}

verify_ingestion_relevance_scenarios() {
  source_url="$1"
  attempt=0
  while :; do
    scenario_count="$(query_db "
      select
        (select count(*) from release_events
          where event_key = 'pypi:urllib3:2.6.0'
            and raw_event->>'schema_version' = 'release-event.v1'
            and raw_event #>> '{package,normalized_name}' = 'urllib3'
            and raw_event #>> '{release,version}' = '2.6.0'
            and raw_event #>> '{observability,analysis_trace_id}' ~ '^[0-9a-f]{32}$')
        +
        (select count(*) from processing_failures
          where source_topic = 'pypi.ingest-failures.v1'
            and error_class = 'malformed_rss'
            and payload->>'input_kind' = 'rss_document'
            and (payload->>'original_bytes')::integer > 0
            and (payload->>'captured_bytes')::integer <= 262144
            and payload->>'content_sha256' ~ '^[0-9a-f]{64}$')
        +
        (select count(*) from processing_failures
          where source_topic = 'pypi.ingest-failures.v1'
            and error_class = 'invalid_release_event'
            and payload->>'input_kind' = 'rss_item'
            and (payload->>'captured_bytes')::integer <= 262144
            and payload->>'content_sha256' ~ '^[0-9a-f]{64}$');
    ")"
    unmonitored_count="$(query_db "
      select
        (select count(*) from release_events
          where event_key = 'pypi:unmonitored-example:1.0.0')
        +
        (select count(*) from processing_failures
          where event_key = 'pypi:unmonitored-example:1.0.0');
    ")"
    metrics="$(curl --fail --silent --max-time 5 --max-filesize 1048576 "${source_url}/metrics" 2>/dev/null || true)"
    relevant_count="$(printf '%s\n' "$metrics" | metric_sum pypi_relevance_events_total 'decision="relevant"')"
    irrelevant_count="$(printf '%s\n' "$metrics" | metric_sum pypi_relevance_events_total 'decision="irrelevant"')"
    invalid_count="$(printf '%s\n' "$metrics" | metric_sum pypi_relevance_events_total 'decision="invalid"')"
    if [ "$scenario_count" -eq 3 ] \
      && [ "$unmonitored_count" -eq 0 ] \
      && [ "$relevant_count" -eq 25 ] \
      && [ "$irrelevant_count" -eq 1 ] \
      && [ "$invalid_count" -eq 2 ]; then
      break
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
      echo "Ingestion scenarios did not reach their exact database and metric outcomes" >&2
      echo "database=${scenario_count:-0} unmonitored=${unmonitored_count:-0} relevant=${relevant_count:-0} irrelevant=${irrelevant_count:-0} invalid=${invalid_count:-0}" >&2
      return 1
    fi
    sleep 2
  done

  release_keys="$(compose exec -T redpanda rpk topic consume pypi.releases.v1 \
    -X brokers=redpanda:9092 --offset start --num 24 --format '%k\n')"
  release_count="$(printf '%s\n' "$release_keys" | awk 'NF { count += 1 } END { print count + 0 }')"
  duplicate_key_count="$(printf '%s\n' "$release_keys" | awk '$0 == "pypi:urllib3:2.6.0" { count += 1 } END { print count + 0 }')"
  if [ "$release_count" -ne 24 ] || [ "$duplicate_key_count" -ne 1 ]; then
    echo "Source dedupe did not publish exactly one release for the duplicate event key" >&2
    echo "release_records=${release_count} duplicate_key_records=${duplicate_key_count}" >&2
    return 1
  fi
}

verify_invalid_worker_release_contract() {
  invalid_payload_sha256="sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
  printf '{}\n' | compose exec -T redpanda rpk topic produce pypi.releases.v1 \
    -X brokers=redpanda:9092 --key i08-invalid-release >/dev/null

  attempt=0
  while :; do
    invalid_failure_count="$(query_db "
      select count(*)
      from processing_failures
      where source_topic = 'pypi.failures.v1'
        and stage = 'ingestion'
        and error_class = 'invalid_release_event'
        and event_key is null
        and payload->>'payload_sha256' = '${invalid_payload_sha256}'
        and raw_failure #>> '{observability,stage_summary,0,detail}' = 'release_topic_contract_invalid';
    ")"
    if [ "$invalid_failure_count" -eq 1 ]; then
      break
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
      echo "Worker did not publish and persist the bounded invalid release failure" >&2
      return 1
    fi
    sleep 2
  done

  wait_for_zero_lag pypi-reasoning-v1
  wait_for_zero_lag pypi-postgres-sink-v1
}

wait_for_exact_release_missing() {
  event_key="pypi:urllib3:0.0.0.post999999999"
  attempt=0
  while :; do
    failure_record="$(query_db "
      with matching_failure as (
        select pf.failure_id, pf.analysis_trace_id
        from processing_failures pf
        join analysis_attempts aa using (failure_id)
        where pf.event_key = '${event_key}'
          and pf.stage = 'enrichment'
          and pf.error_class = 'exact_release_not_found'
          and not pf.retryable
          and pf.attempt_count = 1
          and pf.source_topic = 'pypi.failures.v1'
          and pf.payload #>> '{source_event,event_key}' = '${event_key}'
          and pf.payload->'model_calls' = '[]'::jsonb
          and aa.event_key = '${event_key}'
          and aa.outcome = 'failed'
          and not exists (
            select 1 from model_calls mc
            where mc.processing_attempt_id = aa.processing_attempt_id
          )
      )
      select failure_id::text || '|' || trim(analysis_trace_id)
      from matching_failure
      where (select count(*) from matching_failure) = 1
        and (select count(*) from release_events where event_key = '${event_key}') = 1;
    ")"
    if [ -n "$failure_record" ]; then
      printf '%s\n' "$failure_record"
      return 0
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
      echo "Timed out waiting for one persisted exact_release_not_found failure" >&2
      return 1
    fi
    sleep 2
  done
}

verify_exact_release_missing_api() {
  api_url="$1"
  stats="$(curl --fail --silent --max-time 5 --max-filesize 262144 "${api_url}/api/stats")"
  printf '%s' "$stats" | python3 -c '
import json
import sys

value = json.load(sys.stdin)
assert value["release_event_count"] == 1
assert value["unresolved_failures"] == 1
assert value["estimated_cost_usd"] == 0
assert value["latest_release"]["event_key"] == "pypi:urllib3:0.0.0.post999999999"
assert value["latest_release"]["disposition"] is None
'

  package_history="$(curl --fail --silent --max-time 5 --max-filesize 262144 "${api_url}/api/packages/urllib3")"
  printf '%s' "$package_history" | python3 -c '
import json
import sys

value = json.load(sys.stdin)
assert value["normalized_name"] == "urllib3"
assert value["findings"] == []
assert [release["version"] for release in value["releases"]] == ["0.0.0.post999999999"]
'

  ops="$(curl --fail --silent --max-time 5 --max-filesize 262144 "${api_url}/api/ops/summary")"
  printf '%s' "$ops" | python3 -c '
import json
import sys

value = json.load(sys.stdin)
assert value["attention"]["status"] == "attention"
assert value["attention"]["unresolved_failures"] == 1
assert value["openai"]["estimated_cost_usd"] == 0
'
}

wait_for_attempt() {
  attempt=0
  while :; do
    attempt_record="$(
      query_db "
        select aa.processing_attempt_id || '|' || aa.finding_id || '|' || trim(aa.analysis_trace_id) || '|' || aa.outcome
        from analysis_attempts aa
        where aa.finding_id is not null and aa.outcome = 'publishable'
        order by aa.completed_at desc
        limit 1;
      "
    )"
    if [ -n "${attempt_record}" ]; then
      printf '%s\n' "${attempt_record}"
      return 0
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
      echo "Timed out waiting for the fixture to create a terminal analysis attempt" >&2
      return 1
    fi
    sleep 2
  done
}

wait_for_deterministic_scenarios() {
  attempt=0
  while :; do
    scenario_count="$(query_db "
      with expected(event_key, disposition, publishable, routing, proof) as (
        values
          ('pypi:boto3:1.40.0rc1', 'prerelease_observed', false, 'observe_only', 'prerelease'),
          ('pypi:urllib3:2.6.1', 'non_substantive', false, 'deterministic_non_substantive', 'non_substantive'),
          ('pypi:urllib3:2.6.0', 'publishable', true, 'deterministic_impact', 'python_version'),
          ('pypi:cffi:2.1.0', 'publishable', true, 'deterministic_impact', 'wheel_coverage'),
          ('pypi:packaging:26.2', 'publishable', true, 'deterministic_impact', 'support_expanded'),
          ('pypi:pluggy:1.6.1', 'publishable', true, 'deterministic_impact', 'release_availability')
      )
      select count(*)
      from expected e
      join analysis_runs ar using (event_key)
      where ar.analysis_method = 'deterministic'
        and ar.model_calls = '[]'::jsonb
        and ar.disposition = e.disposition
        and ar.publishable = e.publishable
        and ar.raw_finding #>> '{routing,analysis_eligibility}' = e.routing
        and case e.proof
          when 'prerelease' then ar.disposition = 'prerelease_observed'
          when 'non_substantive' then
            ar.gate_results #>> '{deterministic_triage,decision}' = 'non_substantive'
          when 'support_expanded' then
            ar.gate_results #>> '{deterministic_impact,decision}' = 'support_expanded'
          else ar.gate_results->'deterministic_impact'->'impacts'
            @> jsonb_build_array(jsonb_build_object('dimension', e.proof))
        end
        and not exists (
          select 1
          from analysis_attempts aa
          join model_calls mc using (processing_attempt_id)
          where aa.event_key = e.event_key
        );
    ")"
    if [ "$scenario_count" -eq 6 ]; then
      return 0
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
      echo "Expected six individually verified deterministic pipeline scenarios; found ${scenario_count}" >&2
      return 1
    fi
    sleep 2
  done
}

verify_deterministic_scenario_api() {
  api_url="$1"
  while IFS='|' read -r package version; do
    package_history="$(
      curl --fail --silent --max-time 5 --max-filesize 262144 \
        "${api_url}/api/packages/${package}"
    )"
    if ! printf '%s' "$package_history" | grep -Fq "\"version\":\"${version}\""; then
      echo "Package history API did not return ${package} ${version}" >&2
      return 1
    fi
  done <<'EOF'
boto3|1.40.0rc1
urllib3|2.6.1
urllib3|2.6.0
cffi|2.1.0
packaging|26.2
pluggy|1.6.1
EOF
}

verify_duplicate_terminal_replay() {
  event_key="pypi:urllib3:2.6.0"
  terminal_record="$(query_db "
    select raw_finding::text
    from analysis_runs
    where event_key = '${event_key}'
      and analysis_method = 'deterministic'
      and publishable
      and model_calls = '[]'::jsonb;
  ")"
  if [ -z "$terminal_record" ]; then
    echo "D08 could not locate one deterministic terminal to replay" >&2
    return 1
  fi

  terminal_key="$(printf '%s' "$terminal_record" | python3 -c '
import json
import sys

value = json.load(sys.stdin)
assert value["event_key"] == "pypi:urllib3:2.6.0"
assert value["analysis_metadata"]["model_calls"] == []
print(value["finding_id"])
')"
  before_counts="$(query_db "
    select concat_ws('|',
      (select count(*) from analysis_runs where event_key = '${event_key}'),
      (select count(*) from findings f
        join analysis_runs ar using (finding_id)
        where ar.event_key = '${event_key}'),
      (select count(*) from analysis_attempts where event_key = '${event_key}'),
      (select count(*) from model_calls mc
        join analysis_attempts aa using (processing_attempt_id)
        where aa.event_key = '${event_key}'));
  ")"
  if [ "$before_counts" != "1|1|1|0" ]; then
    echo "D08 precondition expected one persisted zero-model terminal; found ${before_counts}" >&2
    return 1
  fi

  printf '%s\n' "$terminal_record" | compose exec -T redpanda \
    rpk topic produce pypi.findings.v1 -X brokers=redpanda:9092 \
    --key "$terminal_key" >/dev/null
  printf '%s\n' "$terminal_record" | compose exec -T redpanda \
    rpk topic produce pypi.findings.v1 -X brokers=redpanda:9092 \
    --key "$terminal_key" >/dev/null

  wait_for_zero_lag pypi-postgres-sink-v1
  after_counts="$(query_db "
    select concat_ws('|',
      (select count(*) from analysis_runs where event_key = '${event_key}'),
      (select count(*) from findings f
        join analysis_runs ar using (finding_id)
        where ar.event_key = '${event_key}'),
      (select count(*) from analysis_attempts where event_key = '${event_key}'),
      (select count(*) from model_calls mc
        join analysis_attempts aa using (processing_attempt_id)
        where aa.event_key = '${event_key}'));
  ")"
  if [ "$after_counts" != "$before_counts" ]; then
    echo "D08 duplicate terminal changed logical identity counts: ${before_counts} -> ${after_counts}" >&2
    return 1
  fi
  echo "D08 passed: duplicate terminal replay preserved counts ${after_counts} with zero model calls"
}

verify_analysis_version_replay() {
  event_key="pypi:urllib3:2.6.0"
  source_record="$(query_db "
    select raw_event::text from release_events where event_key = '${event_key}';
  ")"
  before_identity="$(query_db "
    select analysis_version || '|' || finding_id
    from analysis_runs
    where event_key = '${event_key}';
  ")"
  before_version="${before_identity%%|*}"
  before_finding="${before_identity#*|}"
  if [ -z "$source_record" ] || [ -z "$before_version" ] || [ -z "$before_finding" ]; then
    echo "D09 could not establish the initial release and analysis identity" >&2
    return 1
  fi

  export ANALYSIS_POLICY_REVISION=analysis-policy-v2
  compose up -d --no-deps --force-recreate reasoning-worker >/dev/null
  wait_url reasoning-worker "$(compose_url reasoning-worker 8090)/ready"
  printf '%s\n' "$source_record" | compose exec -T redpanda \
    rpk topic produce pypi.releases.v1 -X brokers=redpanda:9092 \
    --key "$event_key" >/dev/null

  attempt=0
  while :; do
    after_counts="$(query_db "
      select concat_ws('|',
        (select count(*) from release_events where event_key = '${event_key}'),
        (select count(*) from analysis_runs where event_key = '${event_key}'),
        (select count(*) from findings f
          join analysis_runs ar using (finding_id)
          where ar.event_key = '${event_key}'),
        (select count(*) from analysis_attempts where event_key = '${event_key}'),
        (select count(*) from model_calls mc
          join analysis_attempts aa using (processing_attempt_id)
          where aa.event_key = '${event_key}'));
    ")"
    if [ "$after_counts" = "1|2|2|2|0" ]; then
      break
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
      echo "D09 expected one release and two zero-model analysis identities; found ${after_counts}" >&2
      return 1
    fi
    sleep 2
  done

  after_identity="$(query_db "
    select analysis_version || '|' || finding_id
    from analysis_runs
    where event_key = '${event_key}'
      and analysis_version <> '${before_version}';
  ")"
  after_version="${after_identity%%|*}"
  after_finding="${after_identity#*|}"
  if [ -z "$after_version" ] || [ "$after_finding" = "$before_finding" ]; then
    echo "D09 policy revision did not create a distinct auditable analysis identity" >&2
    return 1
  fi
  revision_count="$(query_db "
    select count(*) from analysis_runs
    where event_key = '${event_key}'
      and raw_finding #>> '{analysis_metadata,versions,analysis_policy_revision}'
        in ('analysis-policy-v1', 'analysis-policy-v2');
  ")"
  if [ "$revision_count" -ne 2 ]; then
    echo "D09 did not retain both explicit policy revisions" >&2
    return 1
  fi
  wait_for_zero_lag pypi-reasoning-v1
  wait_for_zero_lag pypi-postgres-sink-v1
  echo "D09 passed: one release retained distinct v1 and v2 analysis identities with zero model calls"
}

verify_deterministic_finding_ui_data() {
  api_url="$1"
  finding_ids="$(query_db "
    select string_agg(finding_id, ',')
    from analysis_runs
    where analysis_method = 'deterministic'
      and publishable
      and model_calls = '[]'::jsonb;
  ")"
  if [ -z "$finding_ids" ]; then
    echo "P01 could not locate deterministic publishable findings" >&2
    return 1
  fi
  python3 -c '
import json
import sys
import urllib.parse
import urllib.request

base_url, raw_ids = sys.argv[1:]
records = []
for finding_id in raw_ids.split(","):
    url = base_url + "/api/findings/" + urllib.parse.quote(finding_id, safe="")
    with urllib.request.urlopen(url, timeout=5) as response:
        records.append(json.load(response))

assert records
assert all(record["analysis_method"] == "deterministic" for record in records)
assert all(record["model_calls"] == [] for record in records)
assert all(record["limitations"] for record in records)
proofs = [record["gate_results"]["deterministic_impact"] for record in records]
assert any(
    impact.get("recommended_action") and impact.get("verification")
    for proof in proofs
    for impact in proof.get("impacts", [])
)
assert any(len(proof.get("impacts", [])) > 1 for proof in proofs)
assert any(proof.get("support_expansions") for proof in proofs)
' "$api_url" "$finding_ids"
  echo "P01 data passed: real deterministic API findings expose UI actions, secondary impacts, support additions, limitations, and zero model calls"
}

verify_end_to_end_trace_contract() {
  api_url="$1"
  finding_id="$2"
  trace_id="$3"
  grafana_url="$4"
  linked_count="$(query_db "
    select count(*)
    from analysis_runs ar
    join analysis_attempts aa using (finding_id)
    where ar.finding_id = '${finding_id}'
      and trim(ar.analysis_trace_id) = '${trace_id}'
      and trim(aa.analysis_trace_id) = '${trace_id}'
      and ar.raw_finding #>> '{observability,analysis_trace_id}' = '${trace_id}'
      and ar.stage_summary @> '[{\"stage\":\"relevance\"}]'::jsonb
      and ar.stage_summary @> '[{\"stage\":\"enrichment\"}]'::jsonb
      and ar.stage_summary @> '[{\"stage\":\"routing\"}]'::jsonb;
  ")"
  if [ "$linked_count" -ne 1 ]; then
    echo "P05 did not retain one trace identity across the run, attempt, and terminal" >&2
    return 1
  fi

  trace_summary="$(curl --fail --silent --max-time 5 --max-filesize 262144 \
    "${api_url}/api/findings/${finding_id}/trace-summary")"
  printf '%s' "$trace_summary" | python3 -c '
import json
import sys
from urllib.parse import parse_qs, urlsplit

finding_id, trace_id, grafana_url = sys.argv[1:]
value = json.load(sys.stdin)
assert value["finding_id"] == finding_id
assert value["analysis_trace_id"] == trace_id
assert value["processing_attempt_id"]
assert value["model_calls"] == []
stages = {stage["stage"] for stage in value["stage_summary"]}
assert {"relevance", "enrichment", "routing"} <= stages
urls = value["grafana_urls"]
assert set(urls) == {"trace", "logs", "model_payloads"}

expected = urlsplit(grafana_url)
for url in urls.values():
    actual = urlsplit(url)
    assert actual.scheme == expected.scheme
    assert actual.port == expected.port
    assert actual.hostname in {"127.0.0.1", "localhost", "::1"}

trace_query = parse_qs(urlsplit(urls["trace"]).query)
trace_pane = json.loads(trace_query["panes"][0])["trace"]
assert trace_pane["datasource"] == "tempo"
assert trace_pane["queries"][0]["query"] == trace_id

logs_query = parse_qs(urlsplit(urls["logs"]).query)
logs_pane = json.loads(logs_query["panes"][0])["logs"]
assert logs_pane["datasource"] == "loki"
assert trace_id in logs_pane["queries"][0]["expr"]

model_query = parse_qs(urlsplit(urls["model_payloads"]).query)
assert model_query["var-trace_id"] == [trace_id]
' "$finding_id" "$trace_id" "$grafana_url"
  echo "P05 passed: one trace identity links deterministic movement through persistence, API, Tempo, and Loki"
}

verify_runtime_signals_contract() {
  source_url="$1"
  worker_metrics_url="$2"
  prometheus_url="$3"
  api_url="$4"

  worker_metrics="$(curl --fail --silent --max-time 5 --max-filesize 1048576 \
    "${worker_metrics_url}/metrics")"
  printf '%s' "$worker_metrics" | python3 -c '
import re
import sys

lines = [line for line in sys.stdin if line and not line.startswith("#")]
samples = [line.strip() for line in lines if line.strip()]
message_lines = [line for line in samples if line.startswith("pypi_reasoning_messages_total{")]
assert message_lines
assert sum(float(line.rsplit(" ", 1)[1]) for line in message_lines) > 0
for line in samples:
    name = line.split("{", 1)[0]
    labels = set(re.findall(r"([a-z_]+)=\"", line))
    if name == "pypi_reasoning_messages_total":
        assert labels == {"outcome"}
    elif name == "pypi_reasoning_backpressure_total":
        assert labels == {"kind"}
    elif name == "pypi_reasoning_model_calls_total":
        assert labels == {"purpose", "outcome", "service_tier"}
assert not any(line.startswith("pypi_reasoning_model_calls_total{") for line in samples)
assert not any(line.startswith("pypi_reasoning_backpressure_total{") for line in samples)
'

  source_metrics="$(curl --fail --silent --max-time 5 --max-filesize 1048576 \
    "${source_url}/metrics")"
  malformed_dlq="$(printf '%s\n' "$source_metrics" | metric_sum pypi_ingest_dlq_total 'error_class="malformed_rss"')"
  invalid_dlq="$(printf '%s\n' "$source_metrics" | metric_sum pypi_ingest_dlq_total 'error_class="invalid_release_event"')"
  if [ "$malformed_dlq" -lt 1 ] || [ "$invalid_dlq" -lt 1 ]; then
    echo "P06 expected both bounded ingest-DLQ classes in source metrics" >&2
    return 1
  fi

  stats="$(curl --fail --silent --max-time 5 --max-filesize 262144 "${api_url}/api/stats")"
  printf '%s' "$stats" | python3 -c '
import json
import sys

value = json.load(sys.stdin)
assert value["estimated_cost_usd"] == 0
'

  targets="$(curl --fail --silent --max-time 5 --max-filesize 262144 \
    --get --data-urlencode 'query=up == 1' "${prometheus_url}/api/v1/query")"
  printf '%s' "$targets" | python3 -c '
import json
import sys

value = json.load(sys.stdin)
jobs = {item["metric"].get("job") for item in value["data"]["result"]}
assert {"redpanda", "connect-source", "reasoning-worker", "postgres-exporter"} <= jobs
'
  echo "P06 passed: bounded runtime signals match the zero-model fixture batch"
}

verify_full_stack_drain() {
  if lifecycle_output="$(
    python3 "${SMOKE_PROJECT_DIRECTORY}/infra/lifecycle.py" \
      --root "$SMOKE_PROJECT_DIRECTORY" \
      --compose-executable "$SMOKE_COMPOSE_EXECUTABLE" \
      --project-name "$SMOKE_PROJECT_NAME" \
      --max-polls 30 \
      --poll-interval-seconds 1 \
      --probe-timeout-seconds 5 \
      --drain-timeout-seconds 120 \
      drain 2>&1
  )"; then
    :
  else
    case "$lifecycle_output" in
      *"project state could not be inspected"*) smoke_stage l01_project_inspection_failed ;;
      *"Ingress stop did not complete"*) smoke_stage l01_ingress_stop_failed ;;
      *"Consumer lag did not drain"*) smoke_stage l01_lag_drain_failed ;;
      *) smoke_stage l01_lifecycle_unknown_failed ;;
    esac
    echo "$lifecycle_output" >&2
    return 1
  fi
  case "$lifecycle_output" in
    *"Lifecycle drain completed"*) smoke_stage l01_drain_command_succeeded ;;
    *"already stopped"*)
      smoke_stage l01_project_reported_absent
      return 1
      ;;
    *)
      smoke_stage l01_drain_success_output_invalid
      return 1
      ;;
  esac
  running_services="$(compose ps --status running --services)"
  if printf '%s\n' "$running_services" | grep -Fxq connect-source; then
    smoke_stage l01_ingress_running_after_success
    echo "L01 ingress remained running after the drain" >&2
    return 1
  fi
  for service in reasoning-worker connect-sink connect-trace-bridge api web grafana; do
    if ! printf '%s\n' "$running_services" | grep -Fxq "$service"; then
      echo "L01 unexpectedly stopped ${service}" >&2
      return 1
    fi
  done
  echo "L01 passed: ingress stopped after two complete zero-lag samples and review services remained running"
}

wait_for_worker_stopped() {
  attempt=0
  while compose ps --status running --services | grep -Fxq reasoning-worker; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then
      echo "L02 worker did not stop within 60 seconds" >&2
      return 1
    fi
    sleep 1
  done
  worker_container="$(compose ps -a -q reasoning-worker)"
  exit_code="$(docker --host unix:///var/run/docker.sock container inspect \
    --format '{{.State.ExitCode}}' "$worker_container")"
  if [ "$exit_code" -ne 0 ]; then
    echo "L02 worker exited with status ${exit_code}" >&2
    return 1
  fi
}

disable_worker_restart() {
  worker_container="$(compose ps -q reasoning-worker)"
  docker --host unix:///var/run/docker.sock container update \
    --restart=no "$worker_container" >/dev/null
}

verify_worker_shutdown() {
  smoke_stage l02_idle_stop_requested
  idle_started="$(date +%s)"
  # This disposable scenario tests the process contract independently from the
  # supervisor. Disable its restart policy so a clean exit stays observable.
  disable_worker_restart
  compose kill -s SIGTERM reasoning-worker >/dev/null
  wait_for_worker_stopped
  smoke_stage l02_idle_stopped
  idle_elapsed=$(( $(date +%s) - idle_started ))
  if [ "$idle_elapsed" -ge 60 ]; then
    echo "L02 idle worker shutdown exceeded 60 seconds" >&2
    return 1
  fi
  if curl --fail --silent --max-time 2 "$worker_url/ready" >/dev/null 2>&1; then
    echo "L02 stopped worker remained ready" >&2
    return 1
  fi

  smoke_stage l02_worker_start_requested
  compose up -d --no-deps --force-recreate reasoning-worker >/dev/null
  smoke_stage l02_worker_started
  worker_url="$(compose_url reasoning-worker 8090)"
  wait_url reasoning-worker "$worker_url/ready"
  smoke_stage l02_worker_restarted
  source_record="$(query_db "
    select re.raw_event::text
    from release_events re
    join analysis_runs ar using (event_key)
    where ar.analysis_method = 'model_assisted'
    order by ar.created_at
    limit 1;
  ")"
  if [ -z "$source_record" ]; then
    smoke_stage l02_model_fixture_missing
    echo "L02 requires one model-assisted fixture record" >&2
    return 1
  fi
  smoke_stage l02_model_fixture_selected
  event_key="$(printf '%s' "$source_record" | python3 -c '
import json
import sys
print(json.load(sys.stdin)["event_key"])
')"
  attempts_before="$(query_db "
    select count(*) from analysis_attempts where event_key = '${event_key}';
  ")"
  calls_before="$(query_db "
    select count(*) from model_calls mc
    join analysis_attempts aa using (processing_attempt_id)
    where aa.event_key = '${event_key}';
  ")"

  export FAKE_MODEL_DELAY_SECONDS=10
  compose up -d --no-deps --force-recreate reasoning-worker >/dev/null
  worker_url="$(compose_url reasoning-worker 8090)"
  wait_url delayed-reasoning-worker "$worker_url/ready"
  disable_worker_restart
  smoke_stage l02_delayed_worker_ready
  printf '%s\n' "$source_record" | compose exec -T redpanda \
    rpk topic produce pypi.releases.v1 -X brokers=redpanda:9092 \
    --key "$event_key" >/dev/null
  smoke_stage l02_record_published
  sleep 2

  inflight_started="$(date +%s)"
  compose kill -s SIGTERM reasoning-worker >/dev/null
  wait_for_worker_stopped
  smoke_stage l02_inflight_stopped
  inflight_elapsed=$(( $(date +%s) - inflight_started ))
  if [ "$inflight_elapsed" -ge 60 ]; then
    echo "L02 in-flight worker shutdown exceeded 60 seconds" >&2
    return 1
  fi
  wait_for_zero_lag pypi-reasoning-v1
  smoke_stage l02_reasoning_drained
  wait_for_zero_lag pypi-postgres-sink-v1
  smoke_stage l02_sink_drained
  attempts_after="$(query_db "
    select count(*) from analysis_attempts where event_key = '${event_key}';
  ")"
  calls_after="$(query_db "
    select count(*) from model_calls mc
    join analysis_attempts aa using (processing_attempt_id)
    where aa.event_key = '${event_key}';
  ")"
  if [ "$attempts_after" -ne $((attempts_before + 1)) ] \
    || [ "$calls_after" -ne $((calls_before + 3)) ]; then
    echo "L02 did not finish exactly one three-call in-flight analysis before exit" >&2
    return 1
  fi
  smoke_stage l02_counts_verified
  echo "L02 passed: idle and in-flight SIGTERM completed safely within 60 seconds"
}

wait_for_trace() {
  tempo_url="$1"
  trace_id="$2"
  attempt=0
  trace_observation=""
  while :; do
    trace_json="$(
      curl --fail --silent --max-time 5 --max-filesize 262144 \
        "${tempo_url}/api/traces/${trace_id}" 2>/dev/null || true
    )"
    if trace_observation="$(
      printf '%s' "$trace_json" \
        | python3 -m scripts.ci.tempo_trace --trace-id "$trace_id" 2>/dev/null
    )"; then
      smoke_stage tempo_trace_w1_s1_l1_p1
      return 0
    fi
    trace_marker="$(
      printf '%s' "$trace_observation" | python3 -c '
import json
import sys

try:
    print(json.load(sys.stdin).get("marker", "tempo_trace_w0_s0_l0_p0"))
except json.JSONDecodeError:
    print("tempo_trace_w0_s0_l0_p0")
'
    )"
    smoke_stage "$trace_marker"
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then
      echo "Timed out waiting for the worker, sink, and source link on trace ${trace_id} in Tempo" >&2
      echo "Last structural trace observation: ${trace_observation}" >&2
      return 1
    fi
    sleep 2
  done
}

wait_for_zero_lag() {
  group="$1"
  attempt=0
  zero_observations=0
  while :; do
    group_json="$(compose exec -T redpanda rpk group describe "$group" \
      -X brokers=redpanda:9092 --format json 2>/dev/null || true)"
    if printf '%s' "$group_json" | grep -Eq '"total_lag"[[:space:]]*:[[:space:]]*0'; then
      zero_observations=$((zero_observations + 1))
      if [ "$zero_observations" -ge 2 ]; then
        return 0
      fi
    else
      zero_observations=0
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
      echo "Timed out waiting for two consecutive zero-lag observations in ${group}" >&2
      return 1
    fi
    sleep 2
  done
}

wait_for_loki_log() {
  loki_url="$1"
  project="$2"
  trace_id="$3"
  attempt=0
  while :; do
    log_json="$(
      curl --fail --silent --max-time 5 --max-filesize 262144 \
        --get \
        --data-urlencode "query={compose_project=\"${project}\",service=\"reasoning-worker\"} |= \"${trace_id}\"" \
        "${loki_url}/loki/api/v1/query_range" 2>/dev/null || true
    )"
    if printf '%s' "${log_json}" | grep -Fq "${trace_id}"; then
      return 0
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
      echo "Timed out waiting for the project-scoped reasoning log" >&2
      return 1
    fi
    sleep 2
  done
}

wait_for_prometheus_scrape() {
  prometheus_url="$1"
  attempt=0
  while :; do
    metric_json="$(
      curl --fail --silent --max-time 5 --max-filesize 262144 \
        --get \
        --data-urlencode 'query=up{job="reasoning-worker"} == 1' \
        "${prometheus_url}/api/v1/query" 2>/dev/null || true
    )"
    if printf '%s' "${metric_json}" | grep -Fq '"result":[' \
      && ! printf '%s' "${metric_json}" | grep -Fq '"result":[]'; then
      return 0
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
      echo "Timed out waiting for the reasoning Prometheus target" >&2
      return 1
    fi
    sleep 2
  done
}

smoke_stage resolve_ports
redpanda_url="$(compose_url redpanda 9644)"
console_url="$(compose_url redpanda-console 8080)"
source_url="$(compose_url connect-source 4195)"
sink_url="$(compose_url connect-sink 4196)"
bridge_url="$(compose_url connect-trace-bridge 4197)"
worker_url="$(compose_url reasoning-worker 8090)"
worker_metrics_url="$(compose_url reasoning-worker 8001)"
tempo_url="$(compose_url tempo 3200)"
loki_url="$(compose_url loki 3100)"
prometheus_url="$(compose_url prometheus 9090)"
grafana_url="$(compose_url grafana 3000)"
api_url="$(compose_url api 8000)"
web_url="$(compose_url web 8080)"

smoke_stage service_health
wait_url redpanda "${redpanda_url}/v1/status/ready"
wait_url redpanda-console "${console_url}/api/cluster"
wait_url connect-source "${source_url}/ready"
wait_url connect-sink "${sink_url}/ready"
wait_url connect-trace-bridge "${bridge_url}/ready"
wait_url reasoning-worker "${worker_url}/ready"
wait_url reasoning-metrics "${worker_metrics_url}/metrics"
wait_url tempo "${tempo_url}/ready"
wait_url loki "${loki_url}/ready"
wait_url prometheus "${prometheus_url}/-/ready"
wait_url grafana "${grafana_url}/api/health"
wait_url api "${api_url}/api/health"
wait_url web "$web_url"
wait_url web-api "${web_url}/healthz"
wait_command postgres-exporter \
  compose exec -T postgres-exporter \
  /bin/sh /usr/local/bin/postgres-exporter-health

if [ "$smoke_scenario" = "exact-release-missing" ]; then
  smoke_stage exact_release_mode
  compose_model="$(compose config)"
  printf '%s' "$compose_model" | grep -Fq 'SOURCE_FIXTURE_SCENARIO: exact-release-missing'
  printf '%s' "$compose_model" | grep -Fq 'EVIDENCE_MODE: pypi'
  printf '%s' "$compose_model" | grep -Fq 'MODEL_MODE: fake'

  smoke_stage exact_release_failure
  failure_record="$(wait_for_exact_release_missing)"
  failure_id="${failure_record%%|*}"
  trace_id="${failure_record#*|}"
  if [ -z "$failure_id" ] || [ -z "$trace_id" ]; then
    echo "Exact-release failure identity was incomplete" >&2
    exit 1
  fi

  smoke_stage exact_release_api
  verify_exact_release_missing_api "$api_url"
  smoke_stage exact_release_lag
  wait_for_zero_lag pypi-reasoning-v1
  wait_for_zero_lag pypi-postgres-sink-v1
  wait_for_zero_lag pypi-connect-trace-bridge-v1
  smoke_stage exact_release_trace
  wait_for_trace "$tempo_url" "$trace_id"
  wait_for_loki_log "$loki_url" "$SMOKE_PROJECT_NAME" "$trace_id"
  wait_for_prometheus_scrape "$prometheus_url"
  smoke_stage complete
  echo "Smoke passed: one real PyPI 404 became one persisted exact_release_not_found failure with zero model calls and zero lag"
  exit 0
fi

smoke_stage fixture_mode
if ! compose config | grep -Fq 'source-fixture.yaml'; then
  echo "The deterministic smoke test requires SOURCE_CONFIG=source-fixture.yaml" >&2
  exit 1
fi

smoke_stage terminal_attempt
attempt_record="$(wait_for_attempt)"
processing_attempt_id="${attempt_record%%|*}"
finding_and_trace="${attempt_record#*|}"
finding_id="${finding_and_trace%%|*}"
trace_and_outcome="${finding_and_trace#*|}"
trace_id="${trace_and_outcome%%|*}"
attempt_outcome="${trace_and_outcome#*|}"

if [ "$attempt_outcome" != "publishable" ]; then
  echo "Expected a new publishable fixture attempt, got ${attempt_outcome}" >&2
  exit 1
fi

smoke_stage api_delivery
api_detail="$(curl --fail --silent --max-time 5 --max-filesize 262144 "${api_url}/api/findings/${finding_id}")"
printf '%s' "$api_detail" | grep -Fq "\"finding_id\":\"${finding_id}\""
printf '%s' "$api_detail" | grep -Fq "\"analysis_trace_id\":\"${trace_id}\""

web_api_detail="$(curl --fail --silent --max-time 5 --max-filesize 262144 "${web_url}/api/findings/${finding_id}")"
printf '%s' "$web_api_detail" | grep -Fq "\"finding_id\":\"${finding_id}\""

smoke_stage consumer_lag
wait_for_zero_lag pypi-reasoning-v1
wait_for_zero_lag pypi-postgres-sink-v1
wait_for_zero_lag pypi-connect-trace-bridge-v1

smoke_stage ingestion_relevance
verify_ingestion_relevance_scenarios "$source_url"

smoke_stage invalid_worker_release
verify_invalid_worker_release_contract

smoke_stage fixture_history
fixture_package_count="$(query_db "
  select count(*)
  from (
    select package_name
    from release_events
    where package_name in (
      'boto3', 'urllib3', 'requests', 'setuptools', 'certifi',
      'typing-extensions', 'idna', 'charset-normalizer', 'python-dateutil', 'six'
    )
    group by package_name
    having count(distinct version) >= 2
  ) package_history;
")"
if [ "$fixture_package_count" -ne 10 ]; then
  echo "Expected two fixture releases for each of ten monitored packages; found ${fixture_package_count} complete package histories" >&2
  exit 1
fi

smoke_stage deterministic_scenarios
wait_for_deterministic_scenarios
smoke_stage deterministic_scenario_api
verify_deterministic_scenario_api "$api_url"
if [ "$smoke_scenario" = "duplicate-terminal-replay" ]; then
  smoke_stage d08_duplicate_terminal_replay
  verify_duplicate_terminal_replay
fi
if [ "$smoke_scenario" = "analysis-version-replay" ]; then
  smoke_stage d09_analysis_version_replay
  verify_analysis_version_replay
fi
if [ "$smoke_scenario" = "deterministic-finding-ui" ]; then
  smoke_stage p01_deterministic_finding_ui
  verify_deterministic_finding_ui_data "$api_url"
fi
if [ "$requires_full_observability" -eq 1 ]; then
  smoke_stage tempo_trace
  wait_for_trace "$tempo_url" "$trace_id"
  smoke_stage loki_log
  wait_for_loki_log "$loki_url" "$SMOKE_PROJECT_NAME" "$trace_id"
  smoke_stage prometheus_scrape
  wait_for_prometheus_scrape "$prometheus_url"
fi
if [ "$smoke_scenario" = "end-to-end-trace" ]; then
  smoke_stage p05_end_to_end_trace
  verify_end_to_end_trace_contract "$api_url" "$finding_id" "$trace_id" "$grafana_url"
fi
if [ "$smoke_scenario" = "runtime-signals" ]; then
  smoke_stage p06_runtime_signals
  verify_runtime_signals_contract "$source_url" "$worker_metrics_url" "$prometheus_url" "$api_url"
fi
if [ "$smoke_scenario" = "full-stack-drain" ]; then
  smoke_stage l01_full_stack_drain
  verify_full_stack_drain
fi
if [ "$smoke_scenario" = "worker-shutdown" ]; then
  smoke_stage l02_worker_shutdown
  verify_worker_shutdown
fi

smoke_stage complete
if [ "$requires_full_observability" -eq 1 ]; then
  echo "Smoke passed: fixture data reached Redpanda, reasoning, Postgres, Tempo, Loki, Prometheus, API, and web with zero consumer lag"
else
  echo "Smoke passed: ${smoke_scenario} completed through the isolated fixture pipeline with zero consumer lag"
fi
