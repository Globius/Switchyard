<!--
SPDX-FileCopyrightText: 2026 The Switchyard contributors

SPDX-License-Identifier: Apache-2.0
-->

# 0222. Server name on health checks to TLS pools

Status: Accepted
Date: 2026-10-17

## Context

A pool with `tls: true` checks its members over TLS (`check-ssl`), but the check sent no server name. A member that
picks its certificate by SNI (a shared host, an ingress) answered the check with its default certificate or reset it,
so the member was marked down while real traffic, which does send SNI, worked.

## Decision

`HealthMonitor` gets an optional `sni` (a hostname). The renderer adds `check-sni <name>` to each server line of a TLS
pool that has a monitor:

1. the monitor's `sni`, when set;
2. otherwise the alphabetically first exact host (no `*.` wildcard) of an enabled route that sends to the pool;
3. otherwise nothing, which is the behaviour before this change.

`sni` is not valid on a `udp` monitor and is ignored for pools without TLS. A new nullable column
`health_monitor.sni` (migration 0049) holds it. It appears in the API, the UI monitor form and the OpenTofu provider.

## Consequences

Existing monitors render as before unless their pool has a host route, in which case the check now carries that host
as SNI, which is what the same pool's traffic already sends. Set `sni` explicitly to override the route host.
