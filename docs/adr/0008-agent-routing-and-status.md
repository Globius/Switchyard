# 0008. The SE agent, routing profiles, and status/drift

Status: Accepted

## Context

M3 adds the part of an SE that is not HAProxy: VIP placement (Keepalived or FRR) and a way for the
controller to see what each SE is really running.

## Decisions

1. **`sy-agent` is the only writer of routing config.** Go, gRPC over mTLS. It applies signed bundles
   (Ed25519), writes only under `/etc/switchyard/`, reloads only an allowlisted set of units, restores
   the previous files if a reload fails, and has no shell or arbitrary-command endpoint.
2. **Routing is a profile.** `VrrpProfile` / `BgpProfile` (a discriminated union) are attached to an SE
   group via `routing_profile_id`. Renderers are pure; BGP secrets are resolved from `secret_ref`s at
   plan time and never stored.
3. **Bundles go through the same pipeline as HAProxy config.** The canary pushes HAProxy and the bundle
   together and then checks VRRP state, BGP sessions, withdrawn routes and HAProxy health through the
   agent. A failure reverts both.
4. **Status is polled, not pushed.** A `poll_status` job (scheduled by whichever worker holds a PostgreSQL
   advisory lock) stores one `engine_status` row per engine (RLS-protected). The API serves it at
   `/tenants/{t}/status/engines` and as an SSE stream. The design docs mention LISTEN/NOTIFY; the stream
   polls the table instead, which is simpler and good enough at phase-1 scale. Revisit if the
   poll interval becomes a problem.
5. **Drift is reported, never corrected.** Differences between desired and observed state raise
   `drift.detected` / `drift.resolved` audit events and show in the status API.

## Known limits

- HAProxy drift is detected through the render fingerprint comment. A manual edit that leaves that
  comment untouched is not seen; closing this needs an engine call that returns the running config.
- Agent enrolment is described in ADR 0011.
