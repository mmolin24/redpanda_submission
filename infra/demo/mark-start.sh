#!/bin/sh
set -eu

rm -f /demo-state/started-at
psql "$DATABASE_URL" -Atq -c 'SELECT clock_timestamp();' > /demo-state/started-at
chmod 0444 /demo-state/started-at
echo "Demo fixture run started at $(cat /demo-state/started-at)."
