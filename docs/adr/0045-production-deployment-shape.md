# 0045. Production deployment shape: controller image, Patroni, pgBackRest

Status: Accepted
Date: 2026-10-11

## Context
`docs/design/02-backend-design.md` ("HA and deployment") plans one controller image with two entrypoints,
three controller VMs each running api, worker, PostgreSQL/Patroni and etcd under Docker Compose,
synchronous replication to one standby, and pgBackRest to object storage. Only `deploy/dev` existed
(06 section 2).

## Decision
- `deploy/controller/Dockerfile`: one image, entrypoint `syctl`; default command `serve`, the worker runs
  `syctl worker`, migrations run as a one-shot `syctl db upgrade`. Non-root, health check on `/api/v1/healthz`.
- `deploy/prod/compose.yaml`: one node's stack (etcd, Patroni, migrate, api, worker), configured only by
  `node.env`. Etcd and Patroni use host networking so member addresses are the VM addresses.
  The controller connects with `target_session_attrs=read-write` over all three DB hosts, so it follows the leader.
- `deploy/prod/patroni/`: PostgreSQL 17 + Patroni 4 + pgBackRest. Synchronous mode on, `archive_command`
  is `pgbackrest archive-push`, replicas are built with pgBackRest first and `basebackup` as fallback. The
  stanza is created in `post_bootstrap`. Per-node values come from `PATRONI_*` and `PGBACKREST_*` variables.
- `deploy/prod/compose.lab.yaml`: the same image as a 3-node cluster on one Docker host, behind a
  lab HAProxy that follows `/primary`, for failover and restore drills. Not for production.
- `protobuf` becomes an explicit controller dependency (the image exposed that it was only present transitively through the dev group).

## Consequences
The ESXi member of the interim placement is the same stack with `PATRONI_NOFAILOVER=true`. The API and
UI are not yet fronted by a VIP or TLS terminator; that comes with the management tenant (next ADR).
Verified locally: image starts healthy; lab cluster elects a leader, one sync standby, backup works,
stopping the leader fails over in about 25 s and `patronictl reinit` rebuilds the old leader.

## Alternatives considered
A DB VIP/HAProxy in production instead of multi-host DSNs: more moving parts, and the
management SE group does not exist before the controller does. Rejected for now.
