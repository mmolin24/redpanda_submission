\! touch /readiness-probe/initialization-in-progress
\! while [ ! -f /readiness-probe/release-initialization ]; do sleep 1; done
