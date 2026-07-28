#!/bin/sh
set -eu

BROKERS="${REDPANDA_BROKERS:-redpanda:9092}"
ADMIN_HOSTS="${REDPANDA_ADMIN_HOSTS:-redpanda:9644}"

create_topic() {
  topic="$1"
  partitions="$2"
  cleanup_policy="$3"

  rpk topic create "$topic" -X "brokers=${BROKERS}" --if-not-exists --partitions "$partitions" --replicas 1
  rpk topic alter-config "$topic" -X "brokers=${BROKERS}" --set "cleanup.policy=${cleanup_policy}"
  rpk topic alter-config "$topic" -X "brokers=${BROKERS}" --set "retention.ms=-1"
  rpk topic alter-config "$topic" -X "brokers=${BROKERS}" --set "max.message.bytes=1048576"
}

create_topic pypi.releases.v1 3 delete
create_topic pypi.ingest-failures.v1 1 delete
create_topic pypi.findings.v1 3 delete
create_topic pypi.failures.v1 1 delete
create_topic otel-traces 3 delete

rpk cluster config set enable_consumer_group_metrics '["group","partition","consumer_lag"]' \
  -X "brokers=${BROKERS}" -X "admin.hosts=${ADMIN_HOSTS}" --no-confirm
rpk cluster config set consumer_group_lag_collection_interval_sec 15 \
  -X "brokers=${BROKERS}" -X "admin.hosts=${ADMIN_HOSTS}" --no-confirm

rpk topic list -X "brokers=${BROKERS}"
