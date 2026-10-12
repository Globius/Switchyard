#!/bin/sh
# Creates a tenant with its networks, address pools, routing profile and SE group from a YAML file (see
# tenant.example.yaml). Usage: sh adopt.sh tenant.yaml
set -eu
cd "$(dirname "$0")"
FILE=${1:?usage: sh adopt.sh tenant.yaml}
export MSYS_NO_PATHCONV=1
docker compose --env-file .env run --rm --no-deps -v "$(cd "$(dirname "$FILE")" && (pwd -W 2>/dev/null || pwd))/$(basename "$FILE"):/tenant.yaml:ro" migrate adopt /tenant.yaml
