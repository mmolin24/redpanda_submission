ARG BASE_IMAGE=grafana/alloy:v1.16.1@sha256:51aeb9d829239345070619dad3edd6873186f913c84f45b365b74574fcb38ec0
FROM busybox:1.37.0-uclibc@sha256:39e0df8c4d65953b55c344f017e1ff2e0031a7454b3c24e6b76d402f207e315a AS health-probe

ARG BASE_IMAGE
FROM ${BASE_IMAGE}

# The pinned Alloy, Tempo, and Loki images do not all include an HTTP client.
# BusyBox wget is copied in solely for container-local readiness probes.
COPY --from=health-probe /bin/wget /bin/wget
