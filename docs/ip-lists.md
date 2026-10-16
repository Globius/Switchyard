# Country and reputation lists

Refuse traffic from countries or from known-bad addresses (ADR 0147).

## Set up the sources (operator)

The controller only fetches from addresses you configure, in the worker environment:

| Variable | Meaning |
| --- | --- |
| `SWITCHYARD_GEO_URL` | URL template with `{cc}`, the lower-case country code. Example: `https://www.ipdeny.com/ipblocks/data/aggregated/{cc}-aggregated.zone`. Or publish your own export of MaxMind data in the same format. |
| `SWITCHYARD_REPUTATION_FEEDS` | JSON `{"name": "url"}`. Example: `{"spamhaus-drop": "https://www.spamhaus.org/drop/drop.txt"}`. Other blocklists can be exported to this format. CrowdSec has its own source: [CrowdSec](crowdsec.md). |

Each answer is a text file with one network or address per line; `#` and `;` start a comment. Redirects are not followed.
Without either variable the job does not run and the lists stay as they are.

## Use them (tenant)

```yaml
policy_sets:
  - name: blocked-countries
    kind: allow_list
    managed_by: geo
    countries: [XX, YY]        # ISO 3166 codes, upper case
  - name: bad-reputation
    kind: allow_list
    managed_by: reputation
    feed: spamhaus-drop        # a feed the operator configured
virtual_services:
  - name: public-site
    deny_list_id: blocked-countries
    # ...
```

`deny_list_id` refuses the list on HTTP (403) and TCP (connection reset) services. On HTTP it judges the client address after
`restore_client_ip_header` has been applied, so behind Cloudflare it is the real client. UDP services cannot use it. A service has
one deny list.

## How it stays current

`sync_ip_lists` runs every six hours and sends changes as a change set from `system:iplists` (canary, revision, approval where the
tenant requires it). The list starts empty and fills at the first run. The job leaves a list alone when the fetch fails, the
answer is empty, it has more than 100,000 networks, or it would lose more than half of a list of 20 or more networks; the worker
logs why.
