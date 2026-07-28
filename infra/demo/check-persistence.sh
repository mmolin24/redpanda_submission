#!/bin/sh
set -eu

started_at="$(cat /demo-state/started-at)"
deadline="$(( $(date +%s) + ${DEMO_GATE_TIMEOUT_SECONDS:-180} ))"

while [ "$(date +%s)" -lt "$deadline" ]; do
  result="$(
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -v started_at="$started_at" -Atq <<'SQL'
SELECT concat_ws('|',
  (
    SELECT count(DISTINCT event_key)
    FROM analysis_attempts
    WHERE started_at >= :'started_at'::timestamptz
      AND completed_at IS NOT NULL
  ),
  (
    SELECT count(DISTINCT aa.event_key)
    FROM analysis_attempts AS aa
    JOIN analysis_runs AS ar USING (finding_id)
    WHERE aa.started_at >= :'started_at'::timestamptz
      AND aa.event_key IN (
        'pypi:boto3:1.40.0rc1',
        'pypi:urllib3:2.6.1',
        'pypi:urllib3:2.6.0',
        'pypi:cffi:2.1.0',
        'pypi:packaging:26.2',
        'pypi:pluggy:1.6.1'
      )
      AND ar.analysis_method = 'deterministic'
      AND ar.model_calls = '[]'::jsonb
  ),
  (
    SELECT count(*)
    FROM processing_failures
    WHERE stage = 'ingestion'
      AND last_failed_at >= :'started_at'::timestamptz
  )
);
SQL
  )"
  IFS='|' read -r attempts deterministic ingest_failures <<EOF
$result
EOF
  if [ "${attempts:-0}" -ge 24 ] && [ "${deterministic:-0}" -eq 6 ] && \
    [ "${ingest_failures:-0}" -ge 2 ]; then
    echo "Fixture persistence verified: ${attempts} releases, ${deterministic} canaries, ${ingest_failures} ingestion failures."
    exit 0
  fi
  sleep 2
done

echo "Fixture persistence gate timed out; live ingress will not start." >&2
exit 1
