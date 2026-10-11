# 0058. Quickstart stack and production guide

Status: Accepted
Date: 2026-10-11

## Context
Everything needed to run Switchyard existed in pieces (`deploy/dev`, the three-node `deploy/prod`, the runbooks, the lab), but
nothing took someone from a clean machine to a first engine, and nothing said what a production install must change.

## Decision
- `deploy/quickstart/` is a single-host Compose (PostgreSQL, OpenBao with a PKI, controller API and worker, the UI proxy, a TLS
  front) and one idempotent `bootstrap.sh` that makes the keys, certificates, AppRole and policy, enrolls the break-glass
  administrator and prints the sign-in. Helpers connect Proxmox (`connect-proxmox.sh`), create a tenant (`adopt.sh`) and hand an
  engine its enrollment material through stdin (`enroll-engine.sh`). It is a test instance: OpenBao is unsealed by a sidecar that
  holds the key, and engines are enrolled by hand until snippet delivery exists (ADR 0011).
- `docs/quickstart.md` walks it; `docs/production.md` lists what to change (HA, OpenBao, TLS and CA, hardening, backups,
  monitoring, upgrades, sizing as starting points) and what has not been proven.
- Container engines (`SWITCHYARD_ALLOW_LXC`) default to on in the quickstart and the lab and off in production (ADR 0076).

## Consequences
The quickstart was run clean and found two bugs, fixed in the same change: the controller's OpenBao policy lacked
`kv/break-glass/*` (403 creating the break-glass administrator), and the UI proxy cached the API's address (502 after an
upgrade). Engine reachability from the quickstart stack was not exercised on this machine (its Docker network is not routable
from the engines); the end-to-end lab (ADR 0056) covers that path with the controller inside the Proxmox network.
