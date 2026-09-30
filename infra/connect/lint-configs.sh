#!/bin/sh
set -eu

image="docker.redpanda.com/redpandadata/connect:4.99.0@sha256:0e1fb0a8c14d8752e3bb56a317ebe0867bdb61873fe2bb3cfcb64f9139589989"
root_dir="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"

for config in source-fixture.yaml source-demo-fixture.yaml source-live.yaml source-history.yaml sink.yaml trace-bridge.yaml; do
  echo "Linting ${config} with ${image}"
  docker run --rm \
    -e DATABASE_URL=postgresql://pypi:pypi_local_only@postgres:5432/pypi_intelligence \
    -e MONITORED_PACKAGES_PATH=/project-config/monitored-packages.json \
    -v "${root_dir}/config/connect:/config:ro" \
    -v "${root_dir}/config:/project-config:ro" \
    -v "${root_dir}/schemas:/schemas:ro" \
    -v "${root_dir}/data:/data:ro" \
    "$image" lint -r /config/source-resources.yaml "/config/${config}"
done

test_dir="$(mktemp -d)"
trap 'rm -rf "${test_dir}"' EXIT HUP INT TERM
mkdir -p "${test_dir}/fixtures"
fixture_once_output="${test_dir}/fixture-once-output.jsonl"
echo "Testing fixture-once.blobl emits each bounded document once per process with ${image}"
printf '{}\n{}\n{}\n' |
  docker run --rm -i \
    -v "${root_dir}/config/connect:/config:ro" \
    -v "${root_dir}/data:/data:ro" \
    "$image" blobl -f /config/fixture-once.blobl >"${fixture_once_output}"
python3 -c '
import pathlib
import sys

output_path = pathlib.Path(sys.argv[1])
output = output_path.read_text()
expected = "\n\n".join(
    pathlib.Path(path).read_text().rstrip("\n") for path in sys.argv[2:]
)
if output.rstrip("\n") != expected or output.count("<?xml") != 1:
    raise SystemExit("fixture mapping must emit each exact bounded document once")
' "${fixture_once_output}" \
  "${root_dir}/data/fixtures/pypi-updates.xml" \
  "${root_dir}/data/fixtures/malformed-pypi-updates.xml"

exact_release_output="${test_dir}/exact-release-output.jsonl"
printf '{}\n{}\n' |
  docker run --rm -i \
    -e SOURCE_FIXTURE_SCENARIO=exact-release-missing \
    -v "${root_dir}/config/connect:/config:ro" \
    -v "${root_dir}/data:/data:ro" \
    "$image" blobl -f /config/fixture-once.blobl >"${exact_release_output}"
python3 -c '
import pathlib
import sys

output = pathlib.Path(sys.argv[1]).read_text().rstrip("\n")
expected = pathlib.Path(sys.argv[2]).read_text().rstrip("\n")
if output != expected:
    raise SystemExit("exact-release-missing fixture must emit exactly once")
' "${exact_release_output}" "${root_dir}/data/fixtures/exact-release-missing.xml"

cp "${root_dir}/config/connect/source-fixture.yaml" "${test_dir}/source-fixture.yaml"
cp "${root_dir}/config/connect/fixture-once.blobl" "${test_dir}/fixture-once.blobl"
cp "${root_dir}/config/connect/source-history.yaml" "${test_dir}/source-history.yaml"
cp "${root_dir}/config/connect/source-resources.yaml" "${test_dir}/source-resources.yaml"
cp "${root_dir}/tests/connect/source-fixture_benthos_test.yaml" "${test_dir}/source-fixture_benthos_test.yaml"
cp "${root_dir}/config/connect/sink.yaml" "${test_dir}/sink.yaml"
cp "${root_dir}/tests/connect/sink_benthos_test.yaml" "${test_dir}/sink_benthos_test.yaml"
cp "${root_dir}/tests/connect/history-config-validation-test.yaml" "${test_dir}/history-config-validation-test.yaml"
cp "${root_dir}/tests/connect/schema-resource-validation-test.yaml" "${test_dir}/schema-resource-validation-test.yaml"
cp "${root_dir}/config/monitored-packages.json" "${test_dir}/monitored-packages.json"
cp "${root_dir}/tests/platform/fixtures/ingest-failure.invalid-new.json" "${test_dir}/fixtures/ingest-failure.invalid-new.json"
cp "${root_dir}/tests/platform/fixtures/ingest-failure.valid-new.json" "${test_dir}/fixtures/ingest-failure.valid-new.json"
awk 'BEGIN { printf "<rss><channel><item>"; for (i = 0; i < 262200; i++) printf "x" }' \
  > "${test_dir}/fixtures/malformed-rss-over-256k.xml"
printf '{not-json' > "${test_dir}/fixtures/corrupt-monitored-packages.json"
printf '{"packages":[]}' > "${test_dir}/fixtures/empty-monitored-packages.json"
printf '{"packages":["urllib3","urllib3"]}' > "${test_dir}/fixtures/duplicate-monitored-packages.json"
printf '{"packages":[42]}' > "${test_dir}/fixtures/schema-invalid-monitored-packages.json"

echo "Testing source-fixture_benthos_test.yaml with ${image}"
docker run --rm \
  -e DATABASE_URL=postgresql://pypi:pypi_local_only@postgres:5432/pypi_intelligence \
  -v "${test_dir}:/config:ro" \
  -v "${root_dir}/schemas:/schemas:ro" \
  -v "${root_dir}/data:/data:ro" \
  "$image" test -r /config/source-resources.yaml /config/source-fixture_benthos_test.yaml /config/sink_benthos_test.yaml

assert_schema_resource_errors() {
  schema_dir="$1"
  description="$2"
  output_file="${test_dir}/schema-resource-test.log"
  echo "Testing ${description} remains outside the DLQ catch path"
  if docker run --rm \
    -e DATABASE_URL=postgresql://pypi:pypi_local_only@postgres:5432/pypi_intelligence \
    -v "${test_dir}:/config:ro" \
    -v "${schema_dir}:/schemas:ro" \
    -v "${root_dir}/data:/data:ro" \
    "$image" test -r /config/source-resources.yaml /config/schema-resource-validation-test.yaml \
    >"${output_file}" 2>&1; then
    echo "Expected Connect processor initialization to reject ${description}" >&2
    exit 1
  fi
  if ! grep -F "failed to init processor" "${output_file}" >/dev/null ||
    ! grep -F "failed to load JSON schema definition" "${output_file}" >/dev/null; then
    echo "Connect failed for an unexpected reason while testing ${description}" >&2
    cat "${output_file}" >&2
    exit 1
  fi
}

missing_schema_dir="${test_dir}/schemas-missing"
corrupt_schema_dir="${test_dir}/schemas-corrupt"
mkdir -p "${missing_schema_dir}" "${corrupt_schema_dir}"
cp "${root_dir}/schemas/"*.json "${missing_schema_dir}/"
cp "${root_dir}/schemas/"*.json "${corrupt_schema_dir}/"
rm "${missing_schema_dir}/release-event.schema.json"
printf '{not-json' > "${corrupt_schema_dir}/release-event.schema.json"
assert_schema_resource_errors "${missing_schema_dir}" "a missing release-event schema"
assert_schema_resource_errors "${corrupt_schema_dir}" "a corrupt release-event schema"
