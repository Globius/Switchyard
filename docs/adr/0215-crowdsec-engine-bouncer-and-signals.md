# 0215. CrowdSec on the engines: bouncer, access log feed and signal sharing

Status: Accepted
Date: 2026-10-16

## Context

ADR 0213 let the controller read CrowdSec decisions as a reputation feed. That is list blocking: a ban reaches the engines at the next
sync, as a change set. Plan item C11 and the plan 01 row ("CrowdSec + HAProxy bouncer") want the real integration: engines that
enforce CrowdSec decisions within seconds, that keep working when the controller or the CrowdSec API is down, per-service control,
and a way for the platform to feed CrowdSec with what its engines see.

## Decisions

1. **A sidecar slot, like the WAF.** The SE image carries the CrowdSec HAProxy SPOA bouncer (`crowdsec-spoa-bouncer`, MIT, a static
   binary from the project's GitHub release, pinned by version and SHA-256 in `se-image/versions.env`) next to `coraza-spoa` and
   `sy-auth`. Its unit is idle until the agent writes `/etc/switchyard/crowdsec/spoa-bouncer.yaml`; the agent starts it then, restarts
   it when the file changes (it reads its configuration once) and stops it when the last file is gone. It listens on
   `127.0.0.1:9001` only (the WAF uses 9000) and runs as its own user under the same hardening as the other sidecars. It pulls the
   decision stream from the operator's CrowdSec local API every 10 seconds and decides from memory, so an engine keeps blocking known
   bans, and keeps serving everything else, if the API or the controller is down.
2. **A policy set kind `crowdsec` (migration 0048).** Attached to an HTTP service through `policy_set_ids`, one per service.
   `mode` is `detect` (the default: the verdict is captured for the access log, nothing is refused: the simulation mode) or `block`
   (403 for a `ban` verdict). `fail_mode` is `open` (default) or `closed` (503 when the bouncer gave no verdict or reported an error).
   `allow_cidrs` and `excluded_paths` are never checked or refused. `timeout_ms` bounds the wait. The client address is the one
   HAProxy has at that point, so behind Cloudflare the real client is judged when the service restores the client address
   (`restore_client_ip_header`, with the Cloudflare allow list). The tenant controls all of this; the administrator controls only the
   connection (decision 5).
3. **What is enforced.** `ban` decisions on `Ip` and `Range` scopes. Captcha decisions are not enforced: the bouncer is configured with
   `fallback_remediation: allow` for them. The bouncer's captcha and AppSec features are not used; they need provider keys per
   host and the AppSec component, and the Coraza WAF (ADR 0140) is the WAF here. Bot challenges stay with the existing JavaScript and
   CAPTCHA gates ([bot and DDoS protection](../features/bot-protection.md)).
4. **The key travels in the bundle only, one per engine.** A bouncer key is read from the secrets backend when an engine's bundle is built and
   written only into the signed, mTLS-delivered bundle file (`0640`, `root:crowdsec-spoa`), like other sidecar secrets. It never enters
   `haproxy.cfg`, the revisions, the Git mirror, PostgreSQL or logs. With **One key per engine** on, each engine reads its own key from
   `kv/system/crowdsec/bouncer-keys/<engine>`; otherwise all engines share the key in Admin > Integrations. CrowdSec's local API cannot
   register a bouncer for a caller (a bouncer key cannot create bouncers; `cscli` or TLS client authentication does), so an operator
   registers an engine's key with `deploy/crowdsec/register-engine.sh add <engine>` when the engine joins and with `revoke` when it
   is retired or the key leaks. Rotating or revoking one engine's key touches only that engine (verified in the end-to-end test: a revoked
   engine stops receiving new bans, and recovers when its key is restored). An engine without its key fails its apply with the command
   to run. Automatic registration would need TLS client-certificate authentication between bouncers and the local API (CrowdSec documents
   it; the engines already have a PKI): a later step. A policy in use without CrowdSec configured fails the apply with a clear message.
5. **Settings in the database, UI and API (ADR 0213).** The local API address, the key (write-only, stored in OpenBao), the origins
   and, new here, `logs_address` live in `system_setting` (no table). System administrators only.
6. **Feeding CrowdSec: the access log feed.** A policy with `share_logs: true` makes the controller add a Vector drop-in
   (`vector/crowdsec.yaml`, a new agent-managed directory; the unit loads it with `--config-dir`) that picks that service's lines out
   of the logs Vector already reads, wraps each in an RFC 3164 syslog header with program `haproxy`, and sends it over UDP to
   `logs_address`. CrowdSec's syslog datasource accepts only UDP and warns that it can lose messages above a few hundred events a second,
   so this is for small and medium sites; high-volume sites should run CrowdSec's agent on files instead. The UDP stream is not
   encrypted: use a private network or a tunnel. The tenant opts in per service; the administrator chooses the destination. The lines
   contain client addresses and request lines (not bodies) and go to the operator's own CrowdSec engine, not to CrowdSec.
7. **A reference deployment of the engine** is `deploy/crowdsec/` (Compose): the official image, `crowdsecurity/haproxy` collection,
   the syslog datasource, the bouncer registered by `BOUNCER_KEY_switchyard`. **Sharing with CrowdSec is off by default**
   (`DISABLE_ONLINE_API=true`: no signals out, no community blocklist in).
8. **Signal sharing and console enrollment are the operator's explicit choice on that engine.** CrowdSec has no API to enroll an engine
   remotely: the key is used by `cscli console enroll` (or `ENROLL_KEY` in the container) and the enrollment must then be accepted in
   the CrowdSec web app. Switchyard does not run commands on other hosts (no remote shell, security rules in `CLAUDE.md`), so enrolling
   is a documented step on the engine, with the key kept in OpenBao and passed through the environment file at deploy time, not a
   button in Switchyard. The runbook states, from CrowdSec's own documentation, what is sent and what is not.
9. **The community blocklist stays out of copied lists (decided by the project owner after reading the EULA).** List sync (ADR 0147/0213)
   copies addresses into policy sets, the database and the Git mirror (which is public). CrowdSec's EULA for the console and community
   data (v2.0.1, 29 Feb 2024; see below) says, in §6.2, that the user may not "distribute or market" the IP addresses made accessible by the
   service, nor use the Assets to provide services for third parties "in any form", SaaS included. Whether that reaches a plain Security Engine,
   or this platform's tenants, is not for this project to decide, so list sync leaves out decisions of origin `CAPI` unless an administrator names
   `CAPI` in the origins. The engines' bouncers read the same decisions live and keep them in memory only, which is how the blocklist is meant to
   be consumed, so tenants still benefit. §13.1.2 also says the user will not "systematically block" connections from addresses in the Assets
   and will adapt its security processes instead: another reason a CrowdSec policy starts in detect mode. This records the reasoning; it is not
   legal advice.
10. **Enrollment is not in the UI.** CrowdSec has no API for console enrollment and the local API does not show an engine's enrollment to a
    bouncer key. The Integrations screen therefore shows a copy-ready setup snippet and the runbook has the steps.

## What CrowdSec's documentation says, and what was not found

Read on 2026-10-16 from docs.crowdsec.net, the hub, the bouncer's repository and crowdsec.net/privacy-policy:

- Sharing: the engine pushes signal meta-data to the Central API: scenario name, hash and version, timestamp, the engine's machine id, the
  offending IP with geolocation when available, plus its list of enabled scenarios and the names and versions of its remediation components and
  engines. Alerts from custom and tainted scenarios are pushed by default; manual decisions when `manual` is on, which enrolling turns on.
  Alert context (HTTP method, status, path, user agent, target host) goes to the console only when `context` is on, which enrolled
  instances get. Source: central API and console pages, alert context page.
- The community blocklist is tiered (3,000 addresses for free engines that do not contribute, 15,000 for contributors, 60,000 for paying
  users); only signals from unmodified Hub scenarios count towards contributing.
- The privacy policy says CrowdSec processes only contextualised IP addresses (date, time and type of incident), keeps them 3 months as
  they are and then degrades them, and names AWS, GCP, Firebase, Slack and Clickup as recipients.
- Licences: the Security Engine and remediation components are MIT; the hub repository is MIT.

**EULA (read 2026-10-16 from booking.crowdsec.net/hubfs/EULA Crowdsec v2 .pdf, v2.0.1):** it governs the CrowdSec Console as a service and says it does
not change the rights to open-source software. Provisions that matter here: §6 (use for the End User's "exclusive internal needs"); §6.2 (no
using the Solution or the Assets to provide services on behalf of third parties, no distributing or marketing the Assets, "in particular" IP
addresses or attack scenarios made accessible by the service); §8 (what an enrolled user transmits is licensed to CrowdSec irrevocably, non-exclusively,
royalty-free, sub-licensably, including commercial use); §13.1 (joint controllers for the IP addresses; do not systematically block); §10 (Community-tier
liability capped at 100 euros, indemnity for third-party claims; French law, Paris courts); §15 (Assets are confidential, shareable in the community).

**Not found, and therefore not claimed:** a Terms of Service page (crowdsec.net/terms answers 404), whether a Security Engine used without the Console is
bound by the EULA, the Community Blocklist's own usage conditions, any rate limit on the Central API, one-key-per-bouncer guidance and a rotation
procedure, the default of `context` before enrolling, and simulation mode's effect on decisions. The operator must read the EULA before enrolling.

## Consequences

- A request pays one SPOE round trip to a local process (the 500 ms processing timeout is an upper bound; `timeout_ms` is the policy's own limit).
- With the shared key, rotating it needs every protected group re-applied; with one key per engine only that engine's group, and the status poller reports a changed bundle as drift until then.
- An engine without the bouncer binary (an old template) will not start it; the apply succeeds and the unit stays down. Rolling the template
  (`docs/runbooks/rolling-upgrade.md`) fixes that. The agent does not yet report the bouncer's state.
- Request bodies are not inspected by CrowdSec here. The AppSec component is a possible later step.
