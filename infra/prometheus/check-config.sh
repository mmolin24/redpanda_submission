#!/bin/sh
set -eu

image="prom/prometheus:v3.12.0@sha256:69f5241418838263316593f7274a304b095c40bcf22e57272865da91bd60a8ac"
root_dir="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"

docker run --rm --entrypoint promtool \
  -v "${root_dir}/infra/prometheus:/etc/prometheus:ro" \
  "${image}" check config /etc/prometheus/prometheus.yaml

docker run --rm --entrypoint promtool \
  -v "${root_dir}/infra/prometheus:/etc/prometheus:ro" \
  "${image}" check rules /etc/prometheus/alerts.yaml

docker run --rm --entrypoint promtool \
  -w /etc/prometheus \
  -v "${root_dir}/infra/prometheus:/etc/prometheus:ro" \
  "${image}" test rules /etc/prometheus/alerts_test.yaml
