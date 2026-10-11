#!/bin/sh
set -eu
mkdir -p /data/pgdata && chmod 700 /data/pgdata
# The stanza is created by `make prod-stanza` / the runbook once the primary is up (needs archiving working).
exec patroni /etc/patroni.yml
