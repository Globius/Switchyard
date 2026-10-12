# ADR 0191: Kubernetes is a supported place to run the controller

Status: Accepted

## Context

Production had one documented shape: three VMs with Docker Compose, Patroni and etcd (ADR 0048), plus the single-host
quickstart. Teams that already run Kubernetes asked for a third option, next to Docker Compose and VMs or LXC containers on
Proxmox. The controller is already stateless apart from PostgreSQL and OpenBao: the API holds no session state, jobs are
claimed with `FOR UPDATE SKIP LOCKED`, and periodic jobs are scheduled by whichever worker holds a PostgreSQL advisory lock
(`jobs/scheduler.py`). That is what a Deployment needs.

## Decision

`deploy/helm/switchyard` is a Helm chart for the controller stack:

- **api**, **worker** and the **proxy** (UI and `/api` behind the security headers of ADR 0053) are Deployments with two
  replicas each, spread over nodes and protected by PodDisruptionBudgets. The API has readiness and liveness probes on
  `/api/v1/healthz`. The worker has no HTTP port; its identity is its pod name.
- **Several workers are safe.** The queue is claimed with `SKIP LOCKED`; the scheduler takes a session-level advisory lock,
  so exactly one worker enqueues periodic jobs, and when it dies the lock goes with its connection and another worker takes
  over. No new election code was needed. The lab shows one lock holder with two workers.
- **Schema migration is a Job per Helm revision** (`<release>-migrate-<revision>`, forward-only, `syctl db upgrade`). The API
  and worker pods have an init container that waits until the database is at the image's head revision, so a rollout never
  serves new code on an old schema, and a fresh install needs no ordering by hand. Hooks were not used: a pre-install hook
  would run before the database exists. Back up before an upgrade (production.md).
- **PostgreSQL** is a CloudNativePG `Cluster` created by the chart (`database.mode: cnpg`, the operator is a prerequisite and
  is not bundled), or an existing database (`external`). The controller connects with the operator's superuser secret, as the
  other deployments connect as `postgres`; row-level security is forced per table in the schema. Backups use CloudNativePG's
  own `backup` spec through `database.cnpg.extraSpec`.
- **OpenBao stays outside the chart.** Its unseal and key ceremony are not something a controller chart should own
  (production.md, section 4). `deploy/helm/bootstrap-test.sh` and `examples/openbao-test.yaml` set up a one-node OpenBao and the
  Secrets for a test install only.
- **Secrets are never templated.** The chart takes the names of Secrets you create (bundle key, mTLS files and CA, the
  AppRole ids, the Proxmox token, TLS certificate) and mounts them read-only. Nothing secret is in the chart, its values or
  the release history.
- **Entry point:** a Service of any type, an Ingress, or both. By default the proxy pod carries an nginx TLS front (engines
  enroll over https only, and browsers need it for the session cookie), exactly as the quickstart does.
- Pods run as uid 10001 with a read-only root file system, no capabilities and the runtime default seccomp profile.

## Consequences

- Kubernetes is a third deployment shape, alongside Compose and VMs/LXC on Proxmox. The engines and Proxmox do not move: the
  controller must reach the engines' management addresses and the Proxmox API from the pods, and engines must reach
  `publicUrl`.
- nginx does not use search domains, so the proxy is given the API service's full name (`clusterDomain` value).
- Every `helm upgrade` creates a migration Job, even without a schema change; finished Jobs are removed after a day.
- Proven on the two-host lab ([lab/2026-10-12-kubernetes-install.md](../lab/2026-10-12-kubernetes-install.md)); not proven:
  a multi-instance database failover, `LoadBalancer` services and Ingress on a real cluster, and an OpenBao outage.
- The chart is linted and rendered with its value sets in CI (`deploy/helm/test.sh`).
