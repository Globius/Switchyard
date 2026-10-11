# 0063. HAProxy Prometheus exporter listener on the management network

Status: Accepted
Date: 2026-10-11

## Context

The service-discovery list (ADR 0061) points Prometheus at `<management address>:8405`, but the rendered `haproxy.cfg` had no
listener there. The configuration is identical on every engine of an SE group, so it cannot name one engine's address.

## Decisions

1. When the SE group has a network with role `mgmt`, the renderer adds `frontend sy_prometheus` on port 8405 (8404 stays the
   local health endpoint). It binds the wildcard (and `[::]` with `v6only` when a management network is IPv6) and refuses with
   `403` any request whose destination address is outside the management networks' CIDRs (`dst` ACL), so the exporter is
   reachable only on the management address, never on a VIP or the backend network.
2. `GET /metrics` is answered by HAProxy's built-in exporter (`http-request use-service prometheus-exporter`); every other path
   is `404`. The frontend does not log.
3. A group without a management network renders no exporter listener, and its golden output is unchanged. Groups deployed by
   the controller always have one.
4. The listener is part of the rendered file, so it is validated by `haproxy -c` (golden case 12) and exercised against a real
   HAProxy in the lab test.
