# M1 kickoff — Core and render

Paste this into Claude Code from the repo root (`X:\Git\switchyard`).

---

Read `CLAUDE.md`, then `docs/design/02-backend-design.md` (Data model, API design, Change pipeline → Render,
Repo layout/testing) and the Migration inventory table in `docs/design/01-replacement-plan.md`.

We're building milestone **M1 — Core and render** of Switchyard. Goal: from data in PostgreSQL, `syctl render`
produces a valid, deterministic `haproxy.cfg` for an SE group. No pushing to SEs, no agent, no UI yet.

Work in this order. Finish each step (code, tests and docstrings, all gates green) before starting the next,
and give me a short summary after each:

1. **Scaffold.** Create `controller/` with `uv` (Python 3.12), `pyproject.toml`, ruff, mypy strict, pytest,
   pre-commit, a `Makefile` (or `justfile`) with `lint`, `typecheck`, `test`, `fmt`. Add `deploy/dev/compose.yaml`
   with PostgreSQL 17. Add `.gitlab-ci.yml` running the same gates. Add a README section "Developing on Windows".
2. **Domain models** (`switchyard/domain`, no I/O): Tenant, Network, VipPool, SeGroup, VirtualService (+ ports,
   routes), Pool (+ members), HealthMonitor, Certificate (metadata only), PolicySet (redirect rules only for now).
   Validators for hostnames, ports, CIDRs, VIP-inside-network, port collisions on a VIP, and cert SANs covering
   VS hostnames (wildcards handled correctly). Hypothesis tests for the validators.
3. **DB schema** (`switchyard/db`): SQLAlchemy models and the first Alembic migration for the tables M1 needs,
   with `version` columns, UUIDv7 ids, unique names per tenant, and RLS policies driven by `app.tenant_ids`.
   Tests against real PostgreSQL (testcontainers) prove tenant A can't read or write tenant B's rows.
4. **HAProxy renderer** (`switchyard/render`): pure functions from a snapshot to `haproxy.cfg`, `crt-list` and
   SNI/host map files. It must cover what the Avi inventory needs: SNI host routing to pools, TLS termination with
   per-host certs, TLS to backends, HTTP→HTTPS redirect, path redirects (the Nextcloud CardDAV/CalDAV case),
   source-IP persistence, HTTP and TCP health checks, multiple extra ports on one VIP, plain L4 TCP services,
   `X-Forwarded-For`, and restoring the client IP from `CF-Connecting-IP` only when the source is in a
   Cloudflare allow list. Output is sorted and deterministic, and every user value is escaped.
5. **Golden tests:** at least 8 cases under `tests/golden/`, including one that mirrors the inventory with
   **made-up hostnames and RFC 5737/RFC 1918 example addresses** (no real data). Each case is validated with
   `haproxy -c` in the `haproxy:3.2` container.
6. **`syctl render`:** Typer CLI that loads one SE group's state from the DB and prints or writes the rendered
   files, plus `syctl seed --example` to load a demo tenant for local testing.
7. **Docs:** `docs/adr/0001`–`000N` for the decisions this milestone locks in (layering, RLS, change-op model
   placeholder, renderer determinism), and a `docs/render.md` explaining how each object maps to HAProxy config.

Out of scope for M1: change sets/apply, Data Plane API, agent, Proxmox, Cloudflare, ACME, auth/OIDC, the UI.
Leave clean interfaces for them, but no stubs that pretend to work.

If the design docs are ambiguous or contradict each other, ask me before choosing. Commit after each step on a
branch `m1-core-render` and open a merge request on GitLab when all steps are done.
