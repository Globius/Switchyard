<div align="center">

# Switchyard

**An open-source replacement for VMware NSX ALB (Avi): multi-tenant HAProxy load balancing on Proxmox, with BGP or VRRP
VIP placement and a change-set based API, CLI and web UI.**

[![CI](https://github.com/Globius/Switchyard/actions/workflows/ci.yml/badge.svg)](https://github.com/Globius/Switchyard/actions/workflows/ci.yml)
[![Docs](https://github.com/Globius/Switchyard/actions/workflows/docs.yml/badge.svg)](https://globius.github.io/Switchyard/)
[![Snyk security](https://snyk.io/test/github/Globius/Switchyard/badge.svg)](https://snyk.io/test/github/Globius/Switchyard)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Last commit](https://img.shields.io/github/last-commit/Globius/Switchyard)](https://github.com/Globius/Switchyard/commits/main)

[Docs](https://globius.github.io/Switchyard/) · [Releases](https://github.com/Globius/Switchyard/releases) · [Issues](https://github.com/Globius/Switchyard/issues) · [Contributing](https://github.com/Globius/Switchyard/blob/main/CONTRIBUTING.md) · [Security](https://github.com/Globius/Switchyard/security)

</div>

## What it is

Switchyard runs HAProxy service engines (SEs) on Proxmox VMs, places VIPs with BGP (FRR) or VRRP (Keepalived), and
keeps desired state in a controller that pushes it to every engine. Engines keep serving and failing over if the
controller is down. Every apply goes through a canary step, and drift is reported, not auto-corrected.

## Key features

- **Change sets:** edit, review the rendered HAProxy diff, then apply; every revision is kept.
- **Multi-tenant:** PostgreSQL row-level security plus service-layer checks.
- **Services:** HTTP(S) and TCP through HAProxy, UDP through Keepalived/IPVS, policies, WAF (Coraza with OWASP CRS).
- **Access and protection:** OIDC, LDAP/SAML, JWT and client-certificate logins in front of services; bot and DDoS protection.
- **Operations:** alerting, telemetry, GSLB on PowerDNS, a Kubernetes connector, an Avi importer and an OpenTofu provider.

The [feature notes](docs/features/index.md) list what each does and where it stops. Not built: multi-site with
remote engines ([ADR 0180](docs/adr/0180-multi-site-and-remote-engines.md), proposed).

## Screenshots

| | |
| --- | --- |
| ![Tenant home: health, failing members, expiring certificates, drafts and revisions](docs/assets/screenshots/dashboard.png) | ![Virtual services with health and certificate expiry](docs/assets/screenshots/virtual-services.png) |
| Tenant home | Virtual services |
| ![A virtual service: routes, live members and metrics](docs/assets/screenshots/virtual-service.png) | ![Reviewing a change set: the rendered HAProxy diff before apply](docs/assets/screenshots/review.png) |
| A virtual service | Reviewing a change set before apply |

The screenshots use a demo dataset against a mocked API; `make ui-screenshots` refreshes them.

## Quick start

Stand up a test instance on its own machine with Docker Compose, connect Proxmox and deploy a first engine; the
[quickstart](docs/quickstart.md) has every step. To work on the code (PowerShell 7, Docker Desktop):

```powershell
winget install astral-sh.uv          # uv downloads Python 3.12 itself
cd controller
uv sync                              # create .venv and install locked dependencies
uv run pytest -m "not docker"        # fast, Docker-free tests
```

The full developer setup, gates and CI tiers are in [CONTRIBUTING.md](CONTRIBUTING.md) and
[docs/contributing.md](docs/contributing.md). The controller also runs in Proxmox VMs, privileged LXC containers or
on Kubernetes with a Helm chart; see the [production guide](docs/production.md).

## Documentation

| Doc | What it covers |
| --- | --- |
| [Quickstart](docs/quickstart.md) | Stand up a test instance and deploy a first engine |
| [Production guide](docs/production.md) | Deployment, network and ports, sizing, security checklist, backups, upgrades |
| [Feature notes](docs/features/index.md) | One page per feature: what, how to use, limits |
| [Whitepapers](docs/whitepapers/index.md) | Architecture, security model, HA and the data plane, multi-tenancy |
| [Operating guide](docs/operating.md) | Processes, configuration, login, `syctl api`, agents, housekeeping |
| [Runbooks](docs/runbooks/README.md) | Restore, rotate the CA, replace or upgrade engines, roll back |
| [API conventions](docs/api.md) | Auth, errors, filters, pagination; live docs at `/api/v1/docs` |
| [Render guide](docs/render.md) | How each object maps to HAProxy configuration |
| [OpenTofu provider](provider/README.md) | Manage a tenant with OpenTofu |
| [Design docs](docs/design/01-replacement-plan.md) · [ADRs](docs/adr/README.md) | Plans and every locked decision |
| [Lab results](docs/lab/2026-10-12-full-pass.md) | What was run on two real Proxmox hosts |
| [Snyk scanning](docs/security/snyk.md) | The GitHub security scans and their one-time setup |

Build the docs site with `uvx --with-requirements docs/requirements.txt mkdocs build --strict` (`make docs`).

## Contributing and security

Read [CONTRIBUTING.md](CONTRIBUTING.md) first; `CLAUDE.md` holds the project rules Claude Code follows. Report
vulnerabilities as described in [SECURITY.md](SECURITY.md).

## License

Apache-2.0 (see `LICENSE` and `LICENSES/`). Third-party notices and SBOMs: [docs/sbom-and-licensing.md](docs/sbom-and-licensing.md).
