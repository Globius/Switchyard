# Switchyard

Multi-tenant load balancing for Proxmox: HAProxy service engines, VIPs placed with VRRP or BGP, and a controller (API,
CLI and web UI) that owns the desired state and pushes it to every engine, canary first. An open-source replacement for
VMware NSX Advanced Load Balancer (Avi).

## Start here

- **Trying it out:** the [quickstart](quickstart.md) stands up a controller on one machine and deploys a first engine.
- **Running it for real:** the [production guide](production.md) covers deployment options, ports, sizing, security,
  backups and upgrades; the [runbooks](runbooks/README.md) cover the procedures.
- **What it can do:** the [feature notes](features/index.md), one page per feature with its limits.
- **How it is built:** the [whitepapers](whitepapers/index.md) and the [why index](why.md), backed by the
  [decision records](adr/README.md).
- **Using the API:** [API conventions](api.md); the live reference is at `/api/v1/docs` on a running controller.
- **What has been proven:** the [lab results](lab/2026-10-12-full-pass.md).
