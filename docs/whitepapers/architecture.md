# Architecture overview

Switchyard is a multi-tenant load-balancing platform for Proxmox, built to replace VMware NSX Advanced Load Balancer (Avi)
with open-source parts. This paper explains how the pieces fit and why they are split the way they are. The design documents
([replacement plan](../design/01-replacement-plan.md), [backend design](../design/02-backend-design.md)) hold the full
specification; the [why index](../why.md) links each decision.

## The two planes

**Control plane: the controller.** One application (Python, FastAPI) with three faces: the REST API, the web UI (React,
served by an nginx proxy) and the `syctl` CLI. It owns the **desired state** of every tenant in PostgreSQL, turns it into
configuration files, and pushes those files to the engines. It also runs background jobs from a queue in PostgreSQL:
deployments, certificate renewals, status polling, discovery, alert evaluation and syncs with external systems.

**Data plane: the service engines (SEs).** Proxmox guests (VMs by default) that carry the traffic:

| Component | Role |
| --- | --- |
| HAProxy 3.2 with the Data Plane API | HTTP and TCP load balancing, TLS, policies; the API receives configuration and certificates |
| Keepalived | VRRP for VIP failover, and IPVS for UDP services |
| FRR | BGP (with BFD) when VIPs are announced to routers |
| `sy-agent` (Go) | applies signed bundles of non-HAProxy files, reports status, withdraws routes when a service is unhealthy |
| Sidecars | Coraza (WAF), oauth2-proxy (login), Vector (logs), exporters (metrics) |

The planes are deliberately loose: **an engine keeps serving and failing over with the controller down.** The controller is
needed to *change* things, never to *serve* them. See [HA and the data plane](ha-data-plane.md).

## Inside the controller

The controller's code depends downward only: `api → services → domain`. The `domain` (models and validators) and `render`
(configuration generators) layers do no I/O, so the same state always produces the same bytes, and both are tested without
a database or network. `adapters` are the only code that talks to the outside (PostgreSQL aside): Proxmox, the Data Plane
API, the agent, OpenBao, ACME CAs, DNS providers, Cloudflare, PowerDNS, Kubernetes, Prometheus and Loki. Each adapter has a
typed interface and a fake for tests.

```text
  UI / syctl / OpenTofu / automation
                │ HTTPS (REST, SSE)
        ┌───────▼────────┐        ┌──────────────┐
        │  API (FastAPI) │──────▶ │  PostgreSQL  │  desired state, revisions, audit, job queue (RLS)
        │  services      │        └──────────────┘
        │  domain/render │        ┌──────────────┐
        │  worker (jobs) │──────▶ │   OpenBao    │  keys, credentials, agent PKI
        └───────┬────────┘        └──────────────┘
                │ mTLS: Data Plane API (5556), sy-agent (7443)
   ┌────────────▼─────────────┐   ┌──────────────────────────┐
   │ SE: HAProxy, FRR,        │   │ SE ...                   │  per SE group, VRRP or BGP
   │ Keepalived, sy-agent     │   │                          │
   └──────────────────────────┘   └──────────────────────────┘
```

## How a change travels

1. A user (or a job such as discovery or certificate renewal) builds a **change set**: create, update and delete operations
   on tenant objects. The API validates each object's shape and then the whole snapshot: references, networks, port
   collisions, certificate coverage, quotas.
2. The diff shows what will change, which services and engines are affected, and any warnings. A tenant may require a second
   person to approve.
3. **Apply** locks the SE group, renders `haproxy.cfg` and the routing and sidecar files, and validates them with the real
   binaries (`haproxy -c`, FRR and Keepalived checks).
4. The files go to **one canary engine** first. It is watched for `canary_seconds`; if HAProxy, a route or the agent reports a
   problem, the apply stops and the canary returns to the previous configuration.
5. Then the rest of the group, and the change becomes a numbered **revision**, mirrored to Git. Any revision can be rolled back
   through the same pipeline.

Status polling compares what each engine runs with the desired state. A difference is **drift**, reported and never fixed
silently; an operator re-applies on purpose.

## Objects

| Object | Meaning |
| --- | --- |
| Tenant | the unit of ownership and isolation |
| Network, VIP pool | Proxmox SDN networks and the address ranges VIPs and engine NICs come from |
| SE group | a set of engines with an HA mode (VRRP or BGP) and a routing profile |
| Virtual service | VIP, ports, protocol, certificates, routes, attached policy sets |
| Pool, health monitor | backends and how they are checked |
| Policy set | shared request handling: redirects, rewrites, limits, WAF, login, protection and more |
| Certificate | server or client-CA material; keys live in OpenBao |

Configuration from these objects is generated, never typed: there are no free-form configuration snippets, and every value
that reaches a file passes a strict validator and the renderer's escaping. See the [render guide](../render.md).

## Integrations around the core

GSLB on PowerDNS, a Kubernetes connector, pool discovery from Proxmox tags, an Avi importer, an OpenTofu provider, ACME and
DNS providers, Cloudflare IP lists, and Prometheus, Loki and Grafana for telemetry. Each one feeds the same change-set
pipeline (or, for DNS records and alerts, is a live, audited tenant object), so nothing reaches an engine around the canary
step. The [feature notes](../features/index.md) cover each one.
