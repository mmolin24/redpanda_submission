#!/bin/sh
set -eu

image="docker.redpanda.com/redpandadata/connect:4.99.0@sha256:0e1fb0a8c14d8752e3bb56a317ebe0867bdb61873fe2bb3cfcb64f9139589989"
redpanda_image="docker.redpanda.com/redpandadata/redpanda:v26.1.13@sha256:ae0a858eddd0538dacbba5696f9be1f590de1fe51283964afc85f5510fa8f32e"
test_run_id="source-metrics-$$"
container="pypi-source-metrics-test-${test_run_id}"
redpanda_container="pypi-source-metrics-redpanda-${test_run_id}"
root_dir="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
. "${root_dir}/infra/db/test-support.sh"
test_dir="$(mktemp -d)"

cleanup() {
  cleanup_status=$?
  trap - EXIT HUP INT TERM
  set +e
  remove_owned_test_container "${container}" "${test_run_id}"
  remove_owned_test_container "${redpanda_container}" "${test_run_id}"
  rm -rf "${test_dir}"
  exit "${cleanup_status}"
}
trap cleanup EXIT HUP INT TERM

# The oversized title survives XML parsing into the item evidence snapshot,
# while the non-PyPI link deterministically creates one invalid_release_event.
awk 'BEGIN {
  printf "<rss version=\"1.0\"><channel><item><title>"
  for (i = 0; i < 270000; i++) printf "x"
  printf "</title><link>https://example.invalid/not-pypi</link><pubDate>Tue, 21 Jul 2026 12:00:00 GMT</pubDate></item></channel></rss>"
}' > "${test_dir}/pypi-updates.xml"

docker run --detach --name "${redpanda_container}" \
  --label "${test_resource_label}=${test_run_id}" \
  --network none \
  "${redpanda_image}" redpanda start \
  --kafka-addr=internal://0.0.0.0:9092 \
  --advertise-kafka-addr=internal://localhost:9092 \
  --rpc-addr=0.0.0.0:33145 \
  --advertise-rpc-addr=localhost:33145 \
  --mode=dev-container --smp=1 --memory=512M --reserve-memory=0M \
  --default-log-level=warn >/dev/null

redpanda_ready=false
for _attempt in $(seq 1 60); do
  if docker exec "${redpanda_container}" rpk cluster health -X brokers=localhost:9092 >/dev/null 2>&1; then
    redpanda_ready=true
    break
  fi
  sleep 1
done
if [ "${redpanda_ready}" != true ]; then
  echo "Disposable Redpanda did not become ready" >&2
  exit 1
fi
docker exec "${redpanda_container}" rpk topic create pypi.ingest-failures.v1 \
  -X brokers=localhost:9092 --partitions 1 --replicas 1 >/dev/null

docker run --detach --name "${container}" \
  --label "${test_resource_label}=${test_run_id}" \
  --network "container:${redpanda_container}" \
  -e SOURCE_METRICS_REDPANDA_BROKERS=localhost:9092 \
  -v "${root_dir}/config/connect:/config:ro" \
  -v "${root_dir}/schemas:/schemas:ro" \
  -v "${root_dir}/data:/data:ro" \
  -v "${test_dir}:/metrics-data:ro" \
  "${image}" run -r /config/source-resources.yaml /config/source-metrics-test.yaml >/dev/null

metric_sum() {
  metric_name="$1"
  required_label="$2"
  awk -v metric_name="${metric_name}" -v required_label="${required_label}" '
    index($0, metric_name "{") == 1 && index($0, required_label) > 0 { total += $NF }
    END { print total + 0 }
  '
}

connection_ready=false
for _attempt in $(seq 1 20); do
  metrics="$(docker exec "${container}" wget -qO- http://localhost:4195/metrics 2>/dev/null || true)"
  connection_up="$(printf '%s\n' "${metrics}" | metric_sum output_connection_up 'label="source_redpanda_delivery"')"
  if [ "${connection_up}" = 1 ]; then
    connection_ready=true
    break
  fi
  sleep 1
done
if [ "${connection_ready}" != true ]; then
  echo "Pinned Connect output did not establish its Redpanda connection" >&2
  docker logs "${container}" >&2
  exit 1
fi

# The input record is held by the test-only sleep processor. Remove only the
# disposable broker after output initialization, then let the record reach the
# retry boundary.
docker stop "${redpanda_container}" >/dev/null

metrics_ready=false
for _attempt in $(seq 1 40); do
  metrics="$(docker exec "${container}" wget -qO- http://localhost:4195/metrics 2>/dev/null || true)"
  dlq_count="$(printf '%s\n' "${metrics}" | metric_sum pypi_ingest_dlq_total 'error_class="invalid_release_event"')"
  truncated_count="$(printf '%s\n' "${metrics}" | metric_sum pypi_ingest_snapshot_truncated_total 'error_class="invalid_release_event"')"
  output_errors="$(printf '%s\n' "${metrics}" | metric_sum output_error 'label="source_redpanda_delivery"')"
  connection_failures="$(printf '%s\n' "${metrics}" | metric_sum output_connection_failed 'label="source_redpanda_delivery"')"
  connection_lost="$(printf '%s\n' "${metrics}" | metric_sum output_connection_lost 'label="source_redpanda_delivery"')"
  connection_up="$(printf '%s\n' "${metrics}" | metric_sum output_connection_up 'label="source_redpanda_delivery"')"
  delivery_ready="$(printf '%s\n' "${metrics}" | metric_sum pypi_source_delivery_ready_total '')"
  output_sent="$(printf '%s\n' "${metrics}" | metric_sum output_sent 'label="source_redpanda_delivery"')"
  output_error_present=false
  if printf '%s\n' "${metrics}" | grep '^output_error{' | grep -F 'label="source_redpanda_delivery"' >/dev/null; then
    output_error_present=true
  fi
  connection_failed_present=false
  if printf '%s\n' "${metrics}" | grep '^output_connection_failed{' | grep -F 'label="source_redpanda_delivery"' >/dev/null; then
    connection_failed_present=true
  fi
  broker_failure_logged=false
  if docker logs "${container}" 2>&1 | grep -F 'Kafka broker connection failed' >/dev/null; then
    broker_failure_logged=true
  fi
  if [ "${dlq_count}" = 1 ] && [ "${truncated_count}" = 1 ] \
    && [ "${output_error_present}" = true ] && [ "${connection_failed_present}" = true ] \
    && [ "${delivery_ready}" -eq 1 ] && [ "${output_sent}" -eq 0 ] \
    && [ "${broker_failure_logged}" = true ]; then
    metrics_ready=true
    break
  fi
  sleep 1
done
if [ "${metrics_ready}" != true ]; then
  echo "Pinned Connect did not emit the expected DLQ and output metrics" >&2
  echo "dlq=${dlq_count:-0} truncated=${truncated_count:-0} output_error=${output_errors:-0} connection_failed=${connection_failures:-0} connection_lost=${connection_lost:-0} delivery_ready=${delivery_ready:-0} output_sent=${output_sent:-0}" >&2
  printf '%s\n' "${metrics:-}" | grep '^output_' >&2 || true
  docker logs "${container}" >&2
  exit 1
fi

sleep 3
later_metrics="$(docker exec "${container}" wget -qO- http://localhost:4195/metrics)"
later_dlq_count="$(printf '%s\n' "${later_metrics}" | metric_sum pypi_ingest_dlq_total 'error_class="invalid_release_event"')"
later_truncated_count="$(printf '%s\n' "${later_metrics}" | metric_sum pypi_ingest_snapshot_truncated_total 'error_class="invalid_release_event"')"
if [ "${later_dlq_count}" != 1 ] || [ "${later_truncated_count}" != 1 ]; then
  echo "DLQ routing counter changed during output retry" >&2
  exit 1
fi

echo "Pinned Connect emitted stable low-cardinality DLQ and backpressure metrics"
