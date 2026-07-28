#!/bin/sh
set -eu

http_health="$(wget -qO- -T 3 -t 1 http://localhost:8080/admin/health)"
printf '%s\n' "${http_health}" |
  grep -Eq '"isHttpOk"[[:space:]]*:[[:space:]]*true'

cluster_health="$(wget -qO- -T 3 -t 1 http://localhost:8080/api/cluster)"
printf '%s\n' "${cluster_health}" |
  grep -Eq '"brokers"[[:space:]]*:[[:space:]]*\[[[:space:]]*\{'
