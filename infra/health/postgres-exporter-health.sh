#!/bin/sh
set -eu

metrics_file="$(mktemp "${TMPDIR:-/tmp}/postgres-exporter-health.XXXXXX")"
cleanup() {
  rm -f "${metrics_file}"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

wget -qO "${metrics_file}" -T 3 -t 1 http://localhost:9187/metrics

awk '
  $1 == "pg_up" {
    observed = 1
    if ($2 != "1" || NF != 2) {
      unavailable = 1
    }
  }
  END {
    exit !(observed && !unavailable)
  }
' "${metrics_file}"
