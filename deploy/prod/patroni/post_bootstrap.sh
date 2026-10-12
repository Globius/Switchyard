#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Runs once on the first primary: create the application database and the pgBackRest stanza.
set -eu
psql -v ON_ERROR_STOP=1 -h /var/run/postgresql -U postgres -c "CREATE DATABASE switchyard"
pgbackrest --stanza=switchyard stanza-create
