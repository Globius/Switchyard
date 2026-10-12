# Allow, deny, country and reputation lists

**What.** An `allow_list` policy set is a list of networks. A service uses it as an allow list (`allow_list_id`, enforced
with `enforce_allow_list`) or as a deny list (`deny_list_id`). A list can be kept by the controller: `managed_by: cloudflare`
(Cloudflare's ranges), `geo` (countries) or `reputation` (a feed the operator configured).

**How to use.**

```yaml
kind: allow_list
name: block-countries
managed_by: geo
countries: [XX, YY]
```

Attach it as `deny_list_id` on the service. The `sync_ip_lists` job refreshes managed lists every six hours through normal
change sets. The full guide: [Country and reputation lists](../ip-lists.md).

**Limits.** Sources are the operator's (`SWITCHYARD_GEO_URL`, `SWITCHYARD_REPUTATION_FEEDS`), never a tenant's URL. A fetch
that fails, comes back empty, exceeds 100,000 networks or would drop more than half the list leaves the list as it is. An empty
deny list denies nothing; an empty enforced allow list rejects everyone until its first sync.

**Decisions:** [0010](../adr/0010-certificates-and-cloudflare.md), [0147](../adr/0147-country-and-reputation-ip-lists.md).
