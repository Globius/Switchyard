# Feature notes

One page per feature: what it does, how to use it, and where it stops. Each page links the decision records (ADRs) that
fix the details and the longer guide where one exists. Field names are those of the API (`/api/v1/docs` on a running
controller has the full schema); bodies are shown as YAML for reading, and the API takes the same structure as JSON.

Every change to a tenant's load-balancing objects goes through a [change set](change-sets.md), so "how to use" below means
"put this in a change set" (or send it with `?apply=true`, or `syctl api apply`) unless a page says the object changes live.

| Area | Features |
| --- | --- |
| Serving traffic | [Virtual services and routing](virtual-services.md), [Pools and members](pools.md), [Health checks and drain](health-checks.md), [Persistence](persistence.md), [UDP services](udp.md), [HTTP/2, HTTP/3, gRPC and WebSocket](http-protocols.md), [Compression and cache](compression-cache.md) |
| TLS and certificates | [TLS on the front end](tls.md), [Certificates and ACME](certificates.md) |
| Request policies | [Policy sets and request stages](policies.md), [Rate and concurrency limits](rate-limits.md), [Country and reputation lists](ip-lists.md) |
| Security | [WAF](waf.md), [Bot and DDoS protection](bot-protection.md), [Login in front of a service](access-login.md), [API tokens and client certificates](api-protection.md) |
| Placement and HA | [VIP placement: VRRP and BGP](vip-placement.md), [Engines on Proxmox](engines.md), [GSLB](gslb.md) |
| Operating | [Change sets, revisions and drift](change-sets.md), [Alerting](alerting.md), [Metrics and logs](telemetry.md), [Audit log](audit.md), [Tenants, roles and sign-in](tenancy.md) |
| Integrations | [Kubernetes](kubernetes.md), [Pool discovery from Proxmox tags](pool-discovery.md), [Avi importer](avi-import.md), [OpenTofu provider](opentofu.md) |

Not built, on purpose: multi-site and remote engines ([ADR 0180](../adr/0180-multi-site-and-remote-engines.md) is only a
proposal), free-form configuration snippets (excluded in phase 1), and external health-check scripts (engines never run
commands they are sent).
