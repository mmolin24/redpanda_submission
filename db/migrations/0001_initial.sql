BEGIN;

CREATE TABLE IF NOT EXISTS release_events (
  event_key text PRIMARY KEY,
  package_name text NOT NULL,
  version text NOT NULL,
  published_at timestamptz,
  ingested_at timestamptz NOT NULL,
  raw_event jsonb NOT NULL,
  CONSTRAINT release_event_contract
    CHECK (raw_event->>'schema_version' = 'release-event.v1')
);

CREATE INDEX IF NOT EXISTS release_events_package_time_idx
  ON release_events (package_name, published_at DESC NULLS LAST);

CREATE TABLE IF NOT EXISTS analysis_runs (
  finding_id text PRIMARY KEY,
  event_key text NOT NULL REFERENCES release_events(event_key),
  analysis_version text NOT NULL,
  disposition text NOT NULL CHECK (disposition IN (
    'prerelease_observed', 'non_substantive',
    'insufficient_evidence', 'refused', 'publishable',
    'suppressed_low_confidence', 'suppressed_validation_failure'
  )),
  analysis_method text NOT NULL CHECK (
    analysis_method IN ('deterministic', 'model_assisted')
  ),
  processing_priority text CHECK (
    processing_priority IS NULL
    OR processing_priority IN ('high', 'medium', 'low', 'skip')
  ),
  reasoning_complexity text CHECK (
    reasoning_complexity IS NULL
    OR reasoning_complexity IN ('simple', 'moderate', 'complex')
  ),
  materiality_decision text,
  materiality_confidence double precision CHECK (
    materiality_confidence IS NULL OR materiality_confidence BETWEEN 0 AND 1
  ),
  applicability_confidence double precision CHECK (
    applicability_confidence IS NULL OR applicability_confidence BETWEEN 0 AND 1
  ),
  publishable boolean NOT NULL,
  model_calls jsonb NOT NULL DEFAULT '[]'::jsonb,
  gate_results jsonb NOT NULL DEFAULT '{}'::jsonb,
  source_topic text NOT NULL,
  source_partition integer NOT NULL CHECK (source_partition >= 0),
  source_offset bigint NOT NULL CHECK (source_offset >= 0),
  broker_timestamp timestamptz,
  persisted_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL,
  analysis_trace_id char(32) NOT NULL CHECK (analysis_trace_id ~ '^[0-9a-f]{32}$'),
  analysis_span_id char(16) NOT NULL CHECK (analysis_span_id ~ '^[0-9a-f]{16}$'),
  trace_flags char(2) NOT NULL CHECK (trace_flags ~ '^[0-9a-f]{2}$'),
  tracestate text,
  stage_summary jsonb NOT NULL DEFAULT '[]'::jsonb,
  raw_finding jsonb NOT NULL,
  UNIQUE (event_key, analysis_version),
  CONSTRAINT publishable_disposition_consistent
    CHECK (publishable = (disposition = 'publishable'))
);

CREATE INDEX IF NOT EXISTS analysis_runs_trace_idx
  ON analysis_runs (analysis_trace_id);
CREATE INDEX IF NOT EXISTS analysis_runs_disposition_time_idx
  ON analysis_runs (disposition, created_at DESC);
CREATE INDEX IF NOT EXISTS analysis_runs_source_lineage_idx
  ON analysis_runs (source_topic, source_partition, source_offset);

CREATE TABLE IF NOT EXISTS analysis_attempts (
  processing_attempt_id text PRIMARY KEY,
  finding_id text REFERENCES analysis_runs(finding_id) ON DELETE CASCADE,
  failure_id uuid,
  event_key text NOT NULL REFERENCES release_events(event_key),
  analysis_trace_id char(32) NOT NULL CHECK (analysis_trace_id ~ '^[0-9a-f]{32}$'),
  analysis_span_id char(16) NOT NULL CHECK (analysis_span_id ~ '^[0-9a-f]{16}$'),
  trace_flags char(2) NOT NULL CHECK (trace_flags ~ '^[0-9a-f]{2}$'),
  tracestate text,
  outcome text NOT NULL,
  stage_summary jsonb NOT NULL DEFAULT '[]'::jsonb,
  source_topic text NOT NULL,
  source_partition integer NOT NULL CHECK (source_partition >= 0),
  source_offset bigint NOT NULL CHECK (source_offset >= 0),
  started_at timestamptz NOT NULL,
  completed_at timestamptz NOT NULL,
  raw_attempt jsonb NOT NULL,
  CONSTRAINT analysis_attempt_terminal_owner CHECK (
    (finding_id IS NOT NULL AND failure_id IS NULL)
    OR (finding_id IS NULL AND failure_id IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS analysis_attempts_trace_idx
  ON analysis_attempts (analysis_trace_id);
CREATE INDEX IF NOT EXISTS analysis_attempts_finding_time_idx
  ON analysis_attempts (finding_id, completed_at DESC);
CREATE INDEX IF NOT EXISTS analysis_attempts_event_time_idx
  ON analysis_attempts (event_key, completed_at DESC, processing_attempt_id DESC)
  INCLUDE (finding_id);

CREATE TABLE IF NOT EXISTS model_calls (
  model_call_id text NOT NULL,
  processing_attempt_id text NOT NULL
    REFERENCES analysis_attempts(processing_attempt_id) ON DELETE CASCADE,
  logical_purpose text NOT NULL CHECK (logical_purpose IN (
    'materiality_assessment',
    'materiality_correction',
    'materiality_review',
    'applicability_assessment',
    'applicability_correction',
    'customer_impact_summary',
    'customer_impact_correction'
  )),
  physical_attempt integer NOT NULL CHECK (physical_attempt >= 1),
  analysis_trace_id char(32) NOT NULL CHECK (analysis_trace_id ~ '^[0-9a-f]{32}$'),
  span_id char(16) CHECK (span_id IS NULL OR span_id ~ '^[0-9a-f]{16}$'),
  client_request_id uuid NOT NULL,
  openai_request_id text,
  response_id text,
  requested_model text NOT NULL,
  returned_model text,
  requested_service_tier text,
  returned_service_tier text,
  reasoning_effort text,
  outcome text NOT NULL,
  request_payload jsonb,
  response_payload jsonb,
  request_sha256 char(71) CHECK (
    request_sha256 IS NULL OR btrim(request_sha256) ~ '^sha256:[0-9a-f]{64}$'
  ),
  response_sha256 char(71) CHECK (
    response_sha256 IS NULL OR btrim(response_sha256) ~ '^sha256:[0-9a-f]{64}$'
  ),
  prompt_sha256 char(71) CHECK (
    prompt_sha256 IS NULL OR btrim(prompt_sha256) ~ '^sha256:[0-9a-f]{64}$'
  ),
  schema_sha256 char(71) CHECK (
    schema_sha256 IS NULL OR btrim(schema_sha256) ~ '^sha256:[0-9a-f]{64}$'
  ),
  evidence_sha256 char(71) CHECK (
    evidence_sha256 IS NULL OR btrim(evidence_sha256) ~ '^sha256:[0-9a-f]{64}$'
  ),
  input_tokens integer CHECK (input_tokens IS NULL OR input_tokens >= 0),
  cached_input_tokens integer CHECK (
    cached_input_tokens IS NULL OR cached_input_tokens >= 0
  ),
  cache_write_tokens integer NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
  output_tokens integer CHECK (output_tokens IS NULL OR output_tokens >= 0),
  reasoning_tokens integer CHECK (reasoning_tokens IS NULL OR reasoning_tokens >= 0),
  estimated_cost_usd numeric(12,6) CHECK (
    estimated_cost_usd IS NULL OR estimated_cost_usd >= 0
  ),
  cost_table_version text,
  client_duration_ms integer CHECK (
    client_duration_ms IS NULL OR client_duration_ms >= 0
  ),
  openai_processing_ms integer CHECK (
    openai_processing_ms IS NULL OR openai_processing_ms >= 0
  ),
  started_at timestamptz NOT NULL,
  completed_at timestamptz NOT NULL,
  PRIMARY KEY (model_call_id, physical_attempt),
  UNIQUE (processing_attempt_id, logical_purpose, physical_attempt)
);

CREATE INDEX IF NOT EXISTS model_calls_trace_idx
  ON model_calls (analysis_trace_id);
CREATE INDEX IF NOT EXISTS model_calls_openai_request_idx
  ON model_calls (openai_request_id) WHERE openai_request_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS findings (
  finding_id text PRIMARY KEY REFERENCES analysis_runs(finding_id) ON DELETE CASCADE,
  package_name text NOT NULL,
  baseline_version text,
  candidate_version text NOT NULL,
  change_types text[] NOT NULL DEFAULT '{}',
  assessment text,
  evidence_bundle jsonb NOT NULL,
  limitations jsonb NOT NULL DEFAULT '[]'::jsonb,
  published_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS findings_time_idx
  ON findings (published_at DESC);
CREATE INDEX IF NOT EXISTS findings_package_idx
  ON findings (package_name, published_at DESC);

-- Insufficient-evidence terminals remain nonpublishable, but are retained as
-- evidence-limited analyses so operators can inspect what was known and missing.
INSERT INTO findings (
  finding_id, package_name, baseline_version, candidate_version, change_types,
  assessment, evidence_bundle, limitations, published_at
)
SELECT
  ar.finding_id,
  ar.raw_finding->>'package',
  nullif(ar.raw_finding->>'baseline_version', ''),
  ar.raw_finding->>'candidate_version',
  ARRAY(
    SELECT jsonb_array_elements_text(
      coalesce(
        ar.gate_results->'materiality'->'change_types',
        '[]'::jsonb
      )
    )
  ),
  ar.gate_results->'applicability'->>'assessment',
  coalesce(ar.raw_finding->'evidence_bundle', '{}'::jsonb),
  coalesce(
    ar.gate_results->'applicability'->'limitations',
    '[]'::jsonb
  ),
  ar.created_at
FROM analysis_runs ar
WHERE ar.disposition = 'insufficient_evidence'
  AND ar.raw_finding->>'schema_version' = 'finding.v1'
  AND nullif(ar.raw_finding->>'package', '') IS NOT NULL
  AND nullif(ar.raw_finding->>'candidate_version', '') IS NOT NULL
ON CONFLICT (finding_id) DO UPDATE SET
  package_name = EXCLUDED.package_name,
  baseline_version = EXCLUDED.baseline_version,
  candidate_version = EXCLUDED.candidate_version,
  change_types = EXCLUDED.change_types,
  assessment = EXCLUDED.assessment,
  evidence_bundle = EXCLUDED.evidence_bundle,
  limitations = EXCLUDED.limitations,
  published_at = EXCLUDED.published_at;

CREATE TABLE IF NOT EXISTS processing_failures (
  failure_id uuid PRIMARY KEY,
  failure_fingerprint char(71) NOT NULL CHECK (
    btrim(failure_fingerprint) ~ '^sha256:[0-9a-f]{64}$'
  ),
  event_key text,
  stage text NOT NULL,
  error_class text NOT NULL,
  message text NOT NULL,
  retryable boolean NOT NULL,
  attempt_count integer NOT NULL CHECK (attempt_count >= 1),
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  first_failed_at timestamptz NOT NULL,
  last_failed_at timestamptz NOT NULL,
  next_retry_at timestamptz,
  resolved_at timestamptz,
  source_topic text NOT NULL,
  source_partition integer NOT NULL CHECK (source_partition >= 0),
  source_offset bigint NOT NULL CHECK (source_offset >= 0),
  analysis_trace_id char(32) NOT NULL CHECK (analysis_trace_id ~ '^[0-9a-f]{32}$'),
  analysis_span_id char(16) NOT NULL CHECK (analysis_span_id ~ '^[0-9a-f]{16}$'),
  raw_failure jsonb NOT NULL
);

-- The failure table is defined after attempts, so add this reference once both
-- sides of the terminal-attempt relationship exist.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'analysis_attempts'::regclass
      AND conname = 'analysis_attempts_failure_id_fkey'
  ) THEN
    ALTER TABLE analysis_attempts
      ADD CONSTRAINT analysis_attempts_failure_id_fkey
      FOREIGN KEY (failure_id)
      REFERENCES processing_failures(failure_id)
      ON DELETE CASCADE;
  END IF;
END
$$;

CREATE INDEX IF NOT EXISTS processing_failures_unresolved_idx
  ON processing_failures (last_failed_at DESC) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS processing_failures_trace_idx
  ON processing_failures (analysis_trace_id);
CREATE INDEX IF NOT EXISTS processing_failures_unresolved_fingerprint_time_idx
  ON processing_failures (failure_fingerprint, last_failed_at DESC)
  WHERE resolved_at IS NULL;

CREATE OR REPLACE VIEW finding_summaries AS
SELECT
  f.finding_id,
  f.package_name,
  f.baseline_version,
  f.candidate_version,
  f.change_types,
  f.assessment,
  f.published_at,
  ar.event_key,
  ar.disposition,
  ar.analysis_version,
  ar.analysis_trace_id,
  ar.stage_summary,
  ar.created_at
FROM findings f
JOIN analysis_runs ar USING (finding_id);

CREATE OR REPLACE VIEW analysis_trace_summaries AS
SELECT
  ar.finding_id,
  ar.event_key,
  ar.analysis_trace_id,
  ar.analysis_span_id,
  ar.disposition,
  ar.stage_summary,
  ar.created_at,
  ar.persisted_at,
  coalesce(jsonb_agg(
    jsonb_build_object(
      'processing_attempt_id', aa.processing_attempt_id,
      'outcome', aa.outcome,
      'started_at', aa.started_at,
      'completed_at', aa.completed_at,
      'source_topic', aa.source_topic,
      'source_partition', aa.source_partition,
      'source_offset', aa.source_offset
    ) ORDER BY aa.completed_at
  ) FILTER (WHERE aa.processing_attempt_id IS NOT NULL), '[]'::jsonb) AS attempts
FROM analysis_runs ar
LEFT JOIN analysis_attempts aa USING (finding_id)
GROUP BY ar.finding_id;

-- Grafana receives only reviewed operational projections. Raw release,
-- evidence, failure, and model payloads remain application-only.
CREATE OR REPLACE VIEW failure_occurrence_summaries AS
SELECT
  failure_id,
  btrim(failure_fingerprint) AS failure_fingerprint,
  event_key,
  stage,
  error_class,
  message,
  retryable,
  attempt_count,
  first_failed_at,
  last_failed_at,
  next_retry_at,
  resolved_at,
  source_topic,
  source_partition,
  source_offset,
  btrim(analysis_trace_id) AS analysis_trace_id,
  btrim(analysis_span_id) AS analysis_span_id,
  payload @> '{"truncated": true}'::jsonb AS snapshot_truncated
FROM processing_failures;

CREATE OR REPLACE VIEW pipeline_run_summaries AS
SELECT persisted_at, finding_id, event_key, disposition, analysis_trace_id
FROM analysis_runs;

CREATE OR REPLACE VIEW model_call_summaries AS
SELECT
  completed_at,
  model_call_id,
  logical_purpose,
  physical_attempt,
  requested_model,
  requested_service_tier,
  outcome,
  input_tokens,
  cached_input_tokens,
  cache_write_tokens,
  output_tokens,
  reasoning_tokens,
  estimated_cost_usd,
  analysis_trace_id,
  CASE
    WHEN request_payload IS NULL AND response_payload IS NULL THEN 'redacted'
    WHEN request_payload IS NOT NULL AND response_payload IS NULL THEN 'request_only'
    WHEN request_payload IS NOT NULL AND response_payload IS NOT NULL THEN 'captured'
    ELSE 'inconsistent'
  END AS capture_status
FROM model_calls;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_reader') THEN
    CREATE ROLE grafana_reader LOGIN PASSWORD 'grafana_local_only';
  END IF;
END
$$;

ALTER ROLE grafana_reader WITH
  LOGIN
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOINHERIT
  NOREPLICATION
  NOBYPASSRLS
  CONNECTION LIMIT 20;
ALTER ROLE grafana_reader RESET ALL;
ALTER ROLE grafana_reader SET default_transaction_read_only TO on;
ALTER ROLE grafana_reader SET statement_timeout TO '15s';
ALTER ROLE grafana_reader SET lock_timeout TO '5s';
ALTER ROLE grafana_reader SET search_path TO pg_catalog, public;

DO $$
DECLARE
  granted_role name;
BEGIN
  FOR granted_role IN
    SELECT parent_role.rolname
    FROM pg_auth_members membership
    JOIN pg_roles member_role ON member_role.oid = membership.member
    JOIN pg_roles parent_role ON parent_role.oid = membership.roleid
    WHERE member_role.rolname = 'grafana_reader'
  LOOP
    EXECUTE format('REVOKE %I FROM grafana_reader', granted_role);
  END LOOP;
END
$$;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM grafana_reader;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM grafana_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  REVOKE SELECT ON TABLES FROM grafana_reader;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM grafana_reader;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO grafana_reader;

DO $$
BEGIN
  EXECUTE format(
    'REVOKE ALL PRIVILEGES ON DATABASE %I FROM grafana_reader',
    current_database()
  );
  EXECUTE format(
    'REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC',
    current_database()
  );
  EXECUTE format(
    'GRANT CONNECT ON DATABASE %I TO grafana_reader',
    current_database()
  );
END
$$;

GRANT SELECT ON
  pipeline_run_summaries,
  model_call_summaries,
  failure_occurrence_summaries
TO grafana_reader;

COMMIT;
