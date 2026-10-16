# CrowdSec

Block clients that a [CrowdSec](https://www.crowdsec.net/) security engine has banned, on every engine and within seconds, and feed
the engine with what your engines see (ADR 0213, ADR 0215). Operators set the connection up once; tenants then choose per service.

There are two ways to use the bans, and they can be combined:

| | Engine bouncer (recommended) | Reputation list |
| --- | --- | --- |
| How | A CrowdSec policy on the service; the SPOA bouncer on each engine | A `reputation` allow list with the feed `crowdsec`, used as the service's deny list |
| Freshness | About 10 seconds | The sync job (every six hours), as a change set |
| If the controller is down | Keeps enforcing | Keeps the last list |
| Per-service mode (log only, block), fail open or closed | Yes | No |
| Includes the community blocklist (`CAPI`) | Yes, in memory on the engine | No, unless an administrator names `CAPI` in the origins |

## Set up (system administrator)

1. Run a CrowdSec Security Engine. `deploy/crowdsec/` is a ready Compose file with the HAProxy collection and the log receiver; see the
   [CrowdSec runbook](runbooks/crowdsec.md). Register a bouncer: `cscli bouncers add switchyard` (or `BOUNCER_KEY_switchyard` in the
   Compose file). Keep the key it prints.
2. Open **Admin > Integrations > CrowdSec**:
    - **Local API address**, for example `https://crowdsec.example.test:8080`. Every engine must reach it; the bouncer verifies its TLS
      certificate, so use a certificate the engines trust, or plain `http` on a private network.
    - **Bouncer key**. Write-only: the controller stores it in OpenBao and keeps only the reference
      (`kv/system/crowdsec/bouncer-key`). It is never shown again and never kept in the database, the logs, the Git mirror or the HAProxy files;
      it travels to the engines only inside the signed bundle.
    - **Origins to use** (optional): `crowdsec` (your engine's own detections), `CAPI` (the community blocklist), `cscli` (manual bans),
      `lists`. Empty means all for the engine bouncers, and all except `CAPI` for the reputation list.
    - **One key per engine** (optional): see Keys below.
    - **Access log address** (optional): `host:port` (UDP) of the engine's syslog datasource, for services that send their access logs.
   **Set up the CrowdSec engine** (a collapsed panel on the same screen) shows the commands to copy. Enrolling the engine in the CrowdSec console and sharing
   signals is done on the engine, not here (CrowdSec has no remote way to do it); see the [runbook](runbooks/crowdsec.md).
3. **Test connection** reads the decision stream once and reports how many networks are banned. **Sync now** queues the list sync.
   The screen shows the last sync, the number of bans and the last error.

The same settings are available through `GET/PUT /api/v1/system/crowdsec`, `POST /system/crowdsec/test` and `POST /system/crowdsec/sync`,
for system administrators only; every change is in the audit log (`crowdsec.configured`).
Until something is stored, `SWITCHYARD_CROWDSEC_URL`, `_API_KEY`, `_ORIGINS` and `_LOGS_ADDRESS` in the controller environment are used.

Engines need the CrowdSec slot in their template (SE image built with the bouncer binary, `se-image/README.md`). An engine without it
applies the configuration but does not enforce CrowdSec.

## Use (tenant): the CrowdSec policy

Create a **CrowdSec** policy under **Policies > New security policy** and add it to the service's policy sets. As YAML:

```yaml
policy_sets:
  - name: crowdsec
    kind: crowdsec
    mode: detect            # start here: the verdict is logged, nothing is blocked
    fail_mode: open         # closed answers 503 when CrowdSec does not answer
    timeout_ms: 100
    allow_cidrs: [198.51.100.0/24]   # never checked: office, monitoring, partners
    excluded_paths: [/healthz]
    share_logs: false       # true: send this service's access logs to the CrowdSec engine
virtual_services:
  - name: public-site
    policy_set_ids: [crowdsec]
    restore_client_ip_header: CF-Connecting-IP   # behind Cloudflare, with the Cloudflare allow list
```

- **Detect** captures CrowdSec's verdict in the access log (`{ban}` before the quoted request line) and blocks nothing. Read the logs, then
  switch to **Block**, which answers banned clients with `403`.
- **Fail open** serves the request when the bouncer does not answer; **fail closed** answers `503`. A client that is already banned stays
  blocked while the engine's bouncer runs, even if the CrowdSec API is down, because the bouncer decides from memory.
- Only HTTP services can carry the policy, one per service. Only `ban` decisions on addresses and ranges are enforced; captcha decisions are not.
- The client address is the one HAProxy has when the rule runs. Behind a CDN or Cloudflare, restore the real client address on the service
  first (the Cloudflare allow list plus `restore_client_ip_header`), or you will judge the proxy.
- **Send access logs to CrowdSec** (`share_logs`) lets the CrowdSec engine read this service's request logs (client address, time, request
  line, status) to detect attacks and add them to the bans every engine enforces. It is off by default. See the runbook for what leaves the
  engine, where it goes, and what the CrowdSec engine may send onward.

## The reputation list

An allow-list policy set with `managed_by: reputation` and `feed: crowdsec` is filled by the sync job and used as the service's `deny_list_id`
(**Policies > New allow list > Networks come from > CrowdSec bans**). It works without the bouncer on the engines. Details in
[Country and reputation lists](ip-lists.md).

## Limits

- Request bodies are not inspected by CrowdSec here (no AppSec); use the [WAF](features/waf.md).
- CrowdSec's syslog datasource is UDP only and can drop messages above a few hundred events a second; for busy sites feed the engine from files.
- The engine's bouncer state is not shown in the engine view yet; a missing binary or a down bouncer fails open (or closed, per policy).

## Keys

One bouncer key is shared by all engines unless **One key per engine** is on, in which case each engine reads its own from
`kv/system/crowdsec/bouncer-keys/<engine>` and an operator registers it with `deploy/crowdsec/register-engine.sh add <engine>` (CrowdSec cannot create a
bouncer remotely). Rotating or revoking one engine then touches only that engine. See the [runbook](runbooks/crowdsec.md#4-bouncer-keys-shared-or-one-per-engine).

## CrowdSec's terms: plain facts

Read on 2026-10-16. This is a summary of what the documents say, with their section names, so that the people who decide can find the source quickly. It is
not legal advice, and it does not say whether a use is allowed.

- **Where the terms are.** `crowdsec.net/terms` (linked from the docs footer) answers 404. The website footer links a *Legal notice* (about the website),
  the *Privacy policy* and an *EULA*: <https://booking.crowdsec.net/crowdsec-eula> (PDF, "End User License Agreement v2.0.1, 2024/02/29"). The EULA is for the
  **CrowdSec Console** as a service and says it does not change the rights to the open-source software that interacts with it. The Security Engine and
  remediation components are MIT-licensed per CrowdSec's documentation. Whether the EULA binds a Security Engine used without a Console account is not stated in the
  documents read.
- **EULA §3, §4, §10, §16.12.** Community licence: free, "time- and features-limited"; one month, renewing. CrowdSec's liability to Community users is capped at 100 euros, and the user
  indemnifies CrowdSec against third-party claims about the user's activity, including the user's clients. French law, Paris courts.
- **EULA §6 and §6.2.** The right of use is for the End User's "exclusive internal needs". The user must not "use the Solution or the Assets to provide services on behalf of
  third parties ... in any form whatsoever, including SaaS mode", and must not "distribute or market" the Solution or the Assets, "in particular ... IP addresses or attack
  scenarios provided or made accessible by the Service". "Assets" are the information, IP addresses and attack scenarios transmitted to or accessible by the Console.
- **EULA §8.** What an enrolled user transmits (IP addresses, time stamps, attack scenarios) is licensed to CrowdSec irrevocably, non-exclusively, royalty-free and
  sub-licensably, including for commercial use, and the user guarantees the right to do so and compliance with privacy law.
- **EULA §13.1.** CrowdSec and the user are joint controllers for the IP addresses; the user agrees not to "systematically block" connections from addresses in the Assets
  and to adapt its security processes to the additional risk instead.
- **EULA §12, §15.** CrowdSec may audit use remotely. The Assets are Confidential Information, shareable within the CrowdSec community.
- **Privacy policy** (<https://www.crowdsec.net/privacy-policy>). Only contextualised IP addresses (date, time, type of incident) are processed; attacking addresses are kept as
  such for up to 3 months, then degraded in stages (to 16 addresses and 6-hour windows, then 256 and 24-hour windows, then week-level timestamps); some may be kept up to a year
  before anonymisation; recipients named: AWS, GCP, Firebase, Slack, Clickup; the DPO contact is dpo@crowdsec.net.
- **What an engine sends** (docs.crowdsec.net, Central API and console pages): signal meta-data (scenario name, hash and version, time, the engine's machine id, the offending
  address with geolocation when available), the list of enabled scenarios, and the names and versions of engines and remediation components. Alerts from custom and
  tainted scenarios go by default; enrolling also turns on manual decisions and, per the container documentation, `tainted`, `custom` and `context` (HTTP method, status,
  path, user agent, target host of alerts). Access logs are not sent.
- **Community blocklist** (docs.crowdsec.net, Central API, community blocklist). Tiered by size: 3,000 addresses for free engines that do not contribute, 15,000 for regular
  contributors, 60,000 for paid plans; only signals from unmodified Hub scenarios count towards contributing. The page states no usage conditions of its own.

**Not found:** a Terms of Service page, usage conditions for the Community Blocklist beyond the EULA's Assets provisions, rate limits on the Central API, and any statement on
multi-tenant or resale use of the engine itself.

**What this project does about it.** Sharing is off in the reference setup. The reputation-list sync leaves out decisions of origin `CAPI` unless an administrator names `CAPI` in the
origins, because that sync copies addresses into tenant policy sets and a public Git mirror; the engine bouncers read the same decisions live and keep them in memory only.
CrowdSec policies start in detect mode.
