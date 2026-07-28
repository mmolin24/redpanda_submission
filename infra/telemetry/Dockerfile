ARG BASE_IMAGE=grafana/alloy:v1.16.1@sha256:51aeb9d829239345070619dad3edd6873186f913c84f45b365b74574fcb38ec0
FROM busybox:1.38.0-uclibc@sha256:eea4ff5612c911abd1d0e9ed47ba642547b01c3490877d9c1bb5fd6346462da4 AS health-probe

ARG BASE_IMAGE
FROM ${BASE_IMAGE}

# The pinned Alloy, Tempo, and Loki images do not all include an HTTP client.
# BusyBox wget is copied in solely for container-local readiness probes.
COPY --from=health-probe /bin/wget /bin/wget
