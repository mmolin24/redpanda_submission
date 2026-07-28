#!/bin/sh
set -eu

groups="pypi-reasoning-v1 pypi-postgres-sink-v1 pypi-connect-trace-bridge-v1"
max_polls="${DEMO_GATE_MAX_POLLS:-90}"
poll_interval="${DEMO_GATE_POLL_INTERVAL_SECONDS:-2}"

group_is_drained() {
  group="$1"
  payload="$(rpk group describe "$group" -X brokers=redpanda:9092 --format yaml)" || return 1
  lag_values="$(
    printf '%s\n' "$payload" |
      sed -n 's/^  total_lag: \([0-9][0-9]*\)$/\1/p'
  )"
  [ "$(printf '%s\n' "$lag_values" | sed '/^$/d' | wc -l | tr -d ' ')" -eq 1 ] && \
    [ "$lag_values" -eq 0 ] && \
    [ "$(printf '%s\n' "$payload" | grep -c '^    - partition:')" -gt 0 ]
}

poll=0
consecutive_zero_samples=0
while [ "$poll" -lt "$max_polls" ]; do
  all_drained=true
  for group in $groups; do
    if ! group_is_drained "$group"; then
      all_drained=false
    fi
  done
  if [ "$all_drained" = true ]; then
    consecutive_zero_samples="$((consecutive_zero_samples + 1))"
    echo "Zero-lag sample ${consecutive_zero_samples}/2 verified."
    if [ "$consecutive_zero_samples" -eq 2 ]; then
      exit 0
    fi
  else
    consecutive_zero_samples=0
  fi
  poll="$((poll + 1))"
  sleep "$poll_interval"
done

echo "Consumer groups did not produce two consecutive zero-lag samples; live ingress will not start." >&2
exit 1
