#!/bin/sh
# Runs once on the first primary: create the application database and the pgBackRest stanza.
set -eu
psql -v ON_ERROR_STOP=1 -h /var/run/postgresql -U postgres -c "CREATE DATABASE switchyard"
pgbackrest --stanza=switchyard stanza-create
