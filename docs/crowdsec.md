# CrowdSec

Block addresses that a [CrowdSec](https://www.crowdsec.net/) security engine has banned: your own scenarios plus the community
blocklists (ADR 0213). The controller acts as the bouncer. The engines are not changed and never contact CrowdSec.

## Set up (operator)

1. Register a bouncer on the CrowdSec engine: `cscli bouncers add switchyard`. Keep the key it prints.
2. Set, in the worker environment:

| Variable | Meaning |
| --- | --- |
| `SWITCHYARD_CROWDSEC_URL` | Base URL of the CrowdSec local API, for example `https://crowdsec.example.test:8080`. |
| `SWITCHYARD_CROWDSEC_API_KEY` | The bouncer key. Required with the URL. |
| `SWITCHYARD_CROWDSEC_ORIGINS` | Optional, comma separated: only decisions from these origins (for example `crowdsec,CAPI`). Default all. |

This adds the reputation feed `crowdsec`. The name is reserved; it cannot appear in `SWITCHYARD_REPUTATION_FEEDS`.
Only `ban` decisions on an address or a range are used. Captcha and throttle decisions, and country, AS and user scopes, are skipped.

## Use (tenant)

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

## How it stays current

The `sync_ip_lists` job (every six hours) reads the full decision stream and sends the difference as a change set from
`system:iplists`, with canary and approval as for any change. A failed fetch, an empty answer, more than 100,000 networks or a
list that would lose more than half of at least 20 networks leaves the list as it is. Everything else is as in
[Country and reputation lists](ip-lists.md).

This is list-based blocking: a ban reaches the engines after the next sync and the change set, not within seconds.
