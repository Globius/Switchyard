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
    - **Access log address** (optional): `host:port` (UDP) of the engine's syslog datasource, for services that send their access logs.
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
