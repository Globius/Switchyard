# Switchyard

Multi-tenant load balancer controller for Proxmox: HAProxy service engines, BGP/VRRP VIP placement, and a
change-set based API, CLI (`syctl`) and web UI. An open-source replacement for VMware NSX ALB (Avi).

**Status:** milestone M1 (core and render) is implemented: `syctl render` produces a validated,
deterministic `haproxy.cfg` from PostgreSQL. M2 (change sets, API, jobs) and M3 (sy-agent, VRRP/BGP routing, status and drift) are implemented. M4 (Proxmox adapter, IPAM, SDN apply, SE group deploy) is implemented; next: M5 (certificates, Cloudflare).

## Design docs

| Doc | What it covers |
| --- | --- |
| [Replacement plan](docs/design/01-replacement-plan.md) | Avi → open-source feature mapping, architecture, migration |
| [Backend design](docs/design/02-backend-design.md) | Data model, API, change pipeline, agent, jobs, security, milestones |
| [Frontend design](docs/design/03-frontend-design.md) | Screens, change-set UX, frontend stack, Claude Design handoff |
| [Render guide](docs/render.md) | How each object maps to HAProxy configuration |
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
