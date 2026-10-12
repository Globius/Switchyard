# Parity work blocks

Date: 2026-10-12. Splits the phased plan in [08-competitive-parity-plan.md](08-competitive-parity-plan.md) into blocks that
separate threads can own. Item IDs (A1, E1 and so on) are those of 08. Planning only.

## Rules for every block

- One thread owns a block and opens one MR at a time from fresh `origin/main`; merge `main` before each push.
- The backend lands first, then the UI block follows one step behind (block U), per the UI-trails-backend rule.
- Each block adds ADRs from its own range, so numbers never collide. Ranges start at 0100; 0080 to 0099 stay free for
  the threads already running.
- Hot files: `domain/models.py`, `domain/field_rules.py`, `domain/validators.py`, `render/haproxy.py`, `docs/render.md`,
  `api/schemas.py`, `docs/api/openapi.json`. Blocks that touch the same hot file in the same wave merge one after the
  other (smallest first) and rebase; none may reformat code it does not change.
- Every config field passes a strict validator and the renderer escapes it (CLAUDE.md). No free-form snippets.

## Blocks

| Block | Plan items | Owner | Touches | ADRs | Depends on |
| --- | --- | --- | --- | --- | --- |
| **P1 Policy model** | A2, A3, A4 | new thread | new policy set kinds, `RouteMatch`, header and path validators, `render/haproxy.py` request order, `docs/render.md`, golden cases | 0100 to 0104 | none; **gates P3, P4-C2, P8** |
| **P2 Pool and health** | A1, A6, A7, A8 | new thread | `Pool`, `PoolMember`, `HealthMonitor`, persistence kinds, backend lines in the renderer, agent bundle for backend mTLS | 0105 to 0109 | none |
| **P3 Limits** | A5, E10 (login-path rate limits only) | new thread | policy set `kind: rate_limit`, stick-table sizing in quotas, renderer | 0110 to 0114 | P1 (condition model) |
| **P4 Traffic features** | C1, C2, C4, C5, C3 | new thread | port options (QUIC, gRPC, WebSocket), weighted pool routes, SNI passthrough, compression, `peers` | 0115 to 0119 | C2 needs P1; C3 needs P2 (A1); C1, C4, C5 start any time |
| **P5 Telemetry** | B1 | existing Embedded metrics thread | exporter, Prometheus, Vector, Loki, tenant Grafana proxy | its own | none |
| **P6 Alerting and logs** | B2, B3 | new thread | tenant contact model, webhook/mail/chat senders, rules on events and metrics, Loki query API | 0120 to 0124 | P5 |
| **P7 Operate** | B4, B5, C9 (rolling upgrade, scale-out, health score) | existing Operations thread | controller image, production Compose, Patroni, SE image, runbooks, live checks, lab CI | its own | none; **gates P8 (SE image) and P10** |
| **P8 Auth service** | E1, E2, E3, E4 | new thread (after P1 lands) | auth sidecar in the SE image, tenant IdP object, policy set `kind: access`, identity headers, auth audit events | 0130 to 0134 | P1 (A2, A3), P7 SE image, P6 for log fields |
| **P9 Auth extensions** | E5, E6, E7 | same thread as P8 | SAML SP, LDAP/RADIUS, client-cert identity, JWT validation for APIs | 0135 to 0139 | P8 (E7 needs only P1) |
| **P10 WAF** | C6, then E10 (bot, DDoS) | new thread | `waf_policy`, Coraza SPOA in the SE image, detect-first, fail mode | 0140 to 0144 | P5 (logs), P7 (image) |
| **P11 Certificates** | C7 | existing Backend gaps thread | HTTP-01 and TLS-ALPN-01, expiry notifications | its own | notifications need P6 |
| **P12 Integrations** | C9 (tag discovery), C10, C11 | new thread, any time | OpenTofu provider from the OpenAPI, Proxmox-tag pool discovery, geo and reputation lists | 0145 to 0149 | C11 needs P3 |
| **U UI** | form for each backend block | existing Frontend build thread | one UI MR per merged backend block | UI ADRs as needed | trails each block by one step |
| **Later** | D1 GSLB, D2 Kubernetes, D3 Avi importer, D5 to D9, E8, E9 | not started | see 08 section 4 | 0150 and up | decide after phases A to E |

## Waves

| Wave | Blocks that can run in parallel | Collision notes |
| --- | --- | --- |
| 1 (start now) | P1, P2, P5, P7, P11 (ACME part) | P1 and P2 both edit `models.py` and the renderer in different objects; merge the smaller first. P5, P7 and P11 touch other areas. |
| 2 (after P1 merges) | P3, P4, P6 (after P5), P12 | P3 and P4 both add to the renderer's request order; P3 first (smaller). |
| 3 | P8, then P9 | Needs P1 and the SE image from P7. One thread owns both so the auth contract stays in one head. |
| 4 | P10 | Needs logs (P5) and the SE image (P7). |
| Trailing | U for each block | Frontend thread. |

## Gating decisions for Miguel

1. Start waves 1 now? Recommended: P1, P2 and P12 as three new threads; P5, P7, P11 are already running.
2. P8 sidecar choice (oauth2-proxy versus a small Go service in `agent/`). Recommended: oauth2-proxy first (OIDC, headers
   and cookies exist), replace only if SAML, per-route rules or the fail mode need it. Decided in the P8 ADR.
3. WAF (P10) only if a tenant needs it. Recommended: hold until a requirement exists.

## Per-block brief

Each new thread gets this and its plan rows from 08 section 4:

> Scope: the item IDs above. Read CLAUDE.md, 02, `docs/render.md` and the ADRs for the objects you touch. One MR at a
> time, small commits, gates passing (ruff, mypy --strict, pytest, coverage on domain, render and pipeline). New config
> fields: model, validator, field rule, renderer, golden case validated with `haproxy -c`, API schema and OpenAPI,
> `docs/render.md`, ADR from your range. List the UI work in the MR description for the Frontend thread.
