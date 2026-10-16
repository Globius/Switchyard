# CrowdSec

Block addresses that a [CrowdSec](https://www.crowdsec.net/) security engine has banned: your own scenarios plus the community
blocklists (ADR 0213). The controller acts as the bouncer. The engines are not changed and never contact CrowdSec.

## Set up (system administrator)

1. On the CrowdSec engine register a bouncer: `cscli bouncers add switchyard`. Keep the key it prints.
2. In the UI open **Admin > Integrations > CrowdSec** and fill in:
    - **Local API address**, for example `https://crowdsec.example.test:8080`.
    - **Bouncer key**. It is write-only: the controller stores it in OpenBao and keeps only its reference
      (`kv/system/crowdsec/bouncer-key`). It is never shown again, and never kept in the database or the logs.
    - **Origins to use** (optional): `crowdsec` (your own detections), `CAPI` (the community blocklist), `cscli`, `lists`.
      Empty uses all.
3. **Test connection** reads the decision stream once and reports how many networks are banned. **Sync now** queues the list sync
   instead of waiting for the next run.

The same settings are available through `GET/PUT /api/v1/system/crowdsec`, `POST /system/crowdsec/test` and `POST
/system/crowdsec/sync`, all for system administrators only, and every change is in the audit log (`crowdsec.configured`).

Until something is stored, `SWITCHYARD_CROWDSEC_URL`, `SWITCHYARD_CROWDSEC_API_KEY` and `SWITCHYARD_CROWDSEC_ORIGINS` in the
controller environment are used. Saving in the UI stores the settings in the database, which then take precedence.

The screen also shows the **status**: when the stream was last read, how many networks it banned, and the last error. A failed
read leaves the lists as they are.

## Use (tenant)

Create an allow list from **Policies > New allow list > Networks come from > CrowdSec bans**, then pick it as the **Deny list** in the
service settings. As YAML:

```yaml
policy_sets:
  - name: crowdsec-bans
    kind: allow_list
    managed_by: reputation
    feed: crowdsec
virtual_services:
  - name: public-site
    deny_list_id: crowdsec-bans
    # ...
```

Only `ban` decisions on an address or a range are used. Captcha and throttle decisions, and country, AS and user scopes, are skipped.

## How it stays current

The `sync_ip_lists` job (every six hours) reads the full decision stream and sends the difference as a change set from
`system:iplists`, with canary and approval as for any change. A failed fetch, an empty answer, more than 100,000 networks or a
list that would lose more than half of at least 20 networks leaves the list as it is. Everything else is as in
[Country and reputation lists](ip-lists.md).

This is list-based blocking: a ban reaches the engines after the next sync and the change set, not within seconds.
