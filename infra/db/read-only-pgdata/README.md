This directory intentionally masks the `PGDATA` volume declared by the
upstream Postgres image when that image is used by the client-only `db-init`
job. It is mounted read-only and must not contain database state.
