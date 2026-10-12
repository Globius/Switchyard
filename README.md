# Switchyard

Multi-tenant load balancer controller for Proxmox: HAProxy service engines, BGP/VRRP VIP placement, and a
change-set based API, CLI (`syctl`) and web UI. An open-source replacement for VMware NSX ALB (Avi).

**Status:** milestone M1 (core and render) is implemented: `syctl render` produces a validated,
deterministic `haproxy.cfg` from PostgreSQL. M2 (change sets, API, jobs) and M3 (sy-agent, VRRP/BGP routing, status and drift) are implemented. M4 (Proxmox adapter, IPAM, SDN apply, SE group deploy) is implemented; M5 (certificates, ACME, OpenBao, Cloudflare IP sync) is implemented, which completes phase 1 of the backend.

## Design docs

| Doc | What it covers |
| --- | --- |
| [Replacement plan](docs/design/01-replacement-plan.md) | Avi → open-source feature mapping, architecture, migration |
| [Backend design](docs/design/02-backend-design.md) | Data model, API, change pipeline, agent, jobs, security, milestones |
| [Frontend design](docs/design/03-frontend-design.md) | Screens, change-set UX, frontend stack, Claude Design handoff |
| [Quickstart](docs/quickstart.md) | Stand up a test instance on its own machine, connect Proxmox, deploy a first engine |
| [Production guide](docs/production.md) | Sizing, HA, security checklist, OpenBao, TLS and CA, backups, monitoring, upgrades, what is proven |
| [Runbooks](docs/runbooks/README.md) | Restore the database, rotate the CA, replace an engine, roll back, set up lab hosts |
| [Render guide](docs/render.md) | How each object maps to HAProxy configuration |
| [Pool discovery](docs/pool-discovery.md) | Pools filled from tagged Proxmox guests |
| [Country and reputation lists](docs/ip-lists.md) | Deny traffic by country or reputation feed |
| [OpenTofu provider](provider/README.md) | Manage a tenant with OpenTofu; generated from the OpenAPI |
| [Operating guide](docs/operating.md) | Processes, configuration, login, `syctl api`, agents, housekeeping |
| [API conventions](docs/api.md) | Auth, errors, filters, pagination, health, live events, metrics; live docs at `/api/v1/docs` and `/api/v1/redoc` |
| [Frontend scope](docs/design/04-frontend-scope.md) | Screens, fields, states and the endpoint behind each; what is still not backed |
| [Frontend tickets](docs/design/05-frontend-backend-tickets.md) | Backend tickets and the gap audit |
| [ADRs](docs/adr/README.md) | Decisions locked in so far |

These are snapshots of the living docs; each file links to its online version.

## Developing on Windows

Everything runs from PowerShell 7. Linux-only tools (HAProxy, PostgreSQL, later FRR and Keepalived) run in
Docker, so Docker Desktop (WSL2 backend) is the only extra requirement. No `make` is needed.

```powershell
winget install astral-sh.uv          # uv downloads Python 3.12 itself
cd controller
uv sync                              # create .venv and install locked dependencies
uv run ruff check .                  # lint            (make lint)
uv run ruff format --check .         #                 (make lint)
uv run mypy                          # type check      (make typecheck)
uv run pytest                        # tests           (make test)
docker compose -f ../deploy/dev/compose.yaml up -d --wait   # local PostgreSQL 17 (make db-up)
uv run pre-commit install            # optional: run the gates on commit (from the repo root)
```

Notes:

- Tests that need PostgreSQL or HAProxy start their own containers with testcontainers, so they behave the
  same on Windows, WSL2 and CI. Run `uv run pytest -m "not docker"` for the fast, Docker-free subset.
- Line endings are LF (`.gitattributes`). Keep `core.autocrlf` off or `input` for this repo.
- On Linux/macOS or WSL2, `make -C controller check` runs all gates, same as `.gitlab-ci.yml`.

## Working with Claude Code

- `CLAUDE.md`: project rules and conventions Claude Code follows.
- `docs/prompts/m1-kickoff.md`: the prompt that starts milestone M1.

## License

TBD.
