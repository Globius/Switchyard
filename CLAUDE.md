# CLAUDE.md — Switchyard

Switchyard is a multi-tenant, open-source replacement for VMware NSX ALB (Avi) on Proxmox:
HAProxy service engines (SEs) on Proxmox VMs, VIPs placed with BGP (FRR) or VRRP (Keepalived),
and a custom controller (API + CLI + web UI) that owns desired state and pushes it to every SE.

Read these before any non-trivial change. They are the spec:

- `docs/design/01-replacement-plan.md`: why, the Avi feature mapping, target architecture, migration
- `docs/design/02-backend-design.md`: data model, API, change pipeline, adapters, agent, jobs, security, milestones
- `docs/design/03-frontend-design.md`: screens, change-set UX, frontend stack, design handoff

If code and design docs disagree, stop and ask. Don't silently pick one. When a decision changes,
update the doc in the same merge request and add an ADR in `docs/adr/`.

## Status

Pre-M1. Nothing is built yet. Milestones: backend M1–M5 (phase 1), UI U1–U4 trailing one step behind.
The current milestone and its task list live in `docs/prompts/` and the GitLab issues.

## Names

| Thing | Name |
| --- | --- |
| Project / repo | `switchyard` |
| Python package (controller) | `switchyard` (in `controller/`) |
| CLI | `syctl` |
| SE agent (Go) | `sy-agent` |
| Files on SEs | `/etc/switchyard/` |
| shadcn private registry | `@switchyard` |

The design docs were written as "ALB Controller"; that means Switchyard.

## Repo layout (target)

```text
controller/        Python: switchyard/{api,domain,services,render,pipeline,adapters,jobs,db}, tests/
agent/             Go: sy-agent
proto/             gRPC definitions shared by controller and agent
ui/                React + TypeScript + shadcn/ui (starts at U1)
se-image/          cloud-init, OpenTofu, package lists for the SE template
deploy/            Docker Compose, Patroni, example configs
docs/              design/, adr/, runbooks/, prompts/
```

Layers depend downward only: `api → services → domain`. `domain` and `render` do no I/O.
`render` is made of pure functions and is deterministic: the same state always produces the same bytes.
`adapters` are the only code that talks to external systems, and each has a typed interface and a fake for tests.

## Stack

- **Controller:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (async) + Alembic, psycopg 3, Jinja2, Typer (for `syctl`).
- **Database:** PostgreSQL 17 with row-level security (RLS) per tenant. The job queue lives in PostgreSQL (`FOR UPDATE SKIP LOCKED`). No Redis.
- **Agent:** Go (stable toolchain), gRPC over mTLS, systemd unit.
- **Data plane (validation targets):** HAProxy 3.2 LTS + Data Plane API v3, FRR, Keepalived. Use the same versions as the SE image.
- **Tooling:** `uv` for Python dependencies and virtualenvs, `ruff` (lint + format), `mypy --strict`, `pytest`, `hypothesis`, `testcontainers`.

## Rules for every change

1. **Tested.** Every change ships with tests. Renderers get golden-file tests (`tests/golden/<case>/input.yaml` → expected
   `haproxy.cfg` etc.). Rendered output is validated by the real binary (`haproxy -c`) in a container.
   DB code is tested against a real PostgreSQL, including RLS isolation tests.
2. **Documented.** Docstrings on every public module, class and function (Google style, with an example for renderers
   and validators). User-visible behavior is documented in `docs/`. New decisions get an ADR.
3. **Gates pass locally before you call something done:** `ruff check`, `ruff format --check`, `mypy --strict`,
   `pytest`, with coverage ≥ 85% on `domain`, `render` and `pipeline`.
4. **Small commits** with clear messages. One milestone task per merge request where practical.

## Security rules (non-negotiable)

- Tenancy is enforced twice: service-layer checks and PostgreSQL RLS. Never disable RLS in application code.
  Admin access uses a separate DB role.
- No secrets in PostgreSQL, logs, test fixtures or the Git mirror. Store `secret_ref`s that point into OpenBao.
- Every user-supplied value that reaches a config file passes a strict validator (hostnames, paths, regex, header names)
  and is escaped by the renderer. No free-form config snippets in phase 1.
- The SE agent writes only under `/etc/switchyard/`, reloads only an allowlisted set of units, and has no remote shell
  or arbitrary-command endpoint.
- No SSH from the controller to SEs.
- Licensing: avoid BSL-licensed HashiCorp tools. Use OpenTofu and OpenBao.

## Environment notes

- The developer works on Windows (PowerShell 7). Anything that needs Linux binaries (HAProxy, FRR, Keepalived,
  PostgreSQL) runs in Docker/testcontainers, so tests behave the same on Windows, WSL2 and CI.
- Line endings are LF (`.gitattributes` enforces this).
- Git remote: self-hosted GitLab (`git.example.com`). Commit as the `claude` service account, not as the user.
- Don't commit the Avi controller backup or anything derived from it that contains IPs, certs or keys.

## Things not to do

- Don't add Redis, Consul, Vault or Terraform.
- Don't make the data plane depend on the controller. SEs must keep serving and failing over if the controller is down.
- Don't auto-correct drift. Report it.
- Don't push config to all SEs at once. Every apply goes through the canary step.
