#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Supervises one Dex per config in $BROKER_DIR/dex/*.yaml (written by `syctl broker render`).
# A new file starts an instance, a changed file restarts it, a removed file stops it.
# Runs in the dex image (busybox sh). See docs/runbooks/login-broker.md and ADR 0137.
set -u
BROKER_DIR=${BROKER_DIR:-/broker}
STATE=/tmp/broker-state
mkdir -p "$STATE" /var/lib/dex

stop_instance() {
  if [ -f "$STATE/$1.pid" ]; then
    kill "$(cat "$STATE/$1.pid")" 2>/dev/null
    rm -f "$STATE/$1.pid" "$STATE/$1.sum"
  fi
}

while true; do
  for file in "$BROKER_DIR"/dex/*.yaml; do
    [ -e "$file" ] || continue
    name=$(basename "$file" .yaml)
    sum=$(cksum < "$file")
    if [ "$(cat "$STATE/$name.sum" 2>/dev/null)" != "$sum" ] || ! kill -0 "$(cat "$STATE/$name.pid" 2>/dev/null)" 2>/dev/null; then
      stop_instance "$name"
      dex serve "$file" &
      echo $! > "$STATE/$name.pid"
      echo "$sum" > "$STATE/$name.sum"
      echo "broker: started $name"
    fi
  done
  for pidfile in "$STATE"/*.pid; do
    [ -e "$pidfile" ] || continue
    name=$(basename "$pidfile" .pid)
    [ -e "$BROKER_DIR/dex/$name.yaml" ] || { stop_instance "$name"; echo "broker: stopped $name"; }
  done
  sleep 10
done
