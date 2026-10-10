# Switchyard

Multi-tenant load balancer controller for Proxmox: HAProxy service engines, BGP/VRRP VIP placement, and a
change-set based API, CLI (`syctl`) and web UI. An open-source replacement for VMware NSX ALB (Avi).

**Status:** design complete, implementation starting at milestone M1.

## Design docs

| Doc | What it covers |
| --- | --- |
| [Replacement plan](docs/design/01-replacement-plan.md) | Avi → open-source feature mapping, architecture, migration |
| [Backend design](docs/design/02-backend-design.md) | Data model, API, change pipeline, agent, jobs, security, milestones |
| [Frontend design](docs/design/03-frontend-design.md) | Screens, change-set UX, frontend stack, Claude Design handoff |

These are snapshots of the living docs; each file links to its online version.

## Working with Claude Code

- `CLAUDE.md`: project rules and conventions Claude Code follows.
- `docs/prompts/m1-kickoff.md`: the prompt that starts milestone M1.

## License

TBD.
