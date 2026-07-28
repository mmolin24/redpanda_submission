#!/bin/sh

test_resource_label="io.pypi-change-intelligence.test-run"

# The official image opens a temporary Postgres server while initializing the
# requested database. PID 1 becomes postgres only after that server is stopped
# and the final server starts, so require both the final process and real SQL.
wait_for_postgres_database() (
  wait_container="$1"
  wait_user="$2"
  wait_database="$3"
  wait_max_attempts="$4"
  wait_attempt=1

  while [ "${wait_attempt}" -le "${wait_max_attempts}" ]; do
    if docker exec "${wait_container}" sh -c \
      'test "$(cat /proc/1/comm)" = postgres' >/dev/null 2>&1 \
      && docker exec "${wait_container}" psql -v ON_ERROR_STOP=1 \
        -U "${wait_user}" -d "${wait_database}" -Atc "SELECT 1" >/dev/null 2>&1; then
      return 0
    fi
    if [ "${wait_attempt}" -lt "${wait_max_attempts}" ]; then
      sleep 1
    fi
    wait_attempt=$((wait_attempt + 1))
  done

  echo "Disposable Postgres database ${wait_database} did not become ready" >&2
  docker logs "${wait_container}" >&2
  return 1
)

remove_owned_test_container() (
  cleanup_container="$1"
  cleanup_run_id="$2"
  cleanup_actual_run_id="$(
    docker inspect --format \
      '{{ index .Config.Labels "io.pypi-change-intelligence.test-run" }}' \
      "${cleanup_container}" 2>/dev/null || true
  )"

  if [ "${cleanup_actual_run_id}" = "${cleanup_run_id}" ]; then
    docker rm -fv "${cleanup_container}" >/dev/null 2>&1 || true
  fi
)
