# Persistence

**What.** Keeps a client on the same member. `persistence.type` on a pool is `source_ip`, `http_cookie`, `header` or `uri`.

**How to use.**

```yaml
persistence: {type: http_cookie, name: SRV, mode: insert, ttl_s: 3600, secure: true, http_only: true}
# or {type: header, header: X-Session}, or {type: uri, source: url_param, param: sid}
```

To share the stick tables between the engines of a group, so a failover keeps the sessions, set `peers` on the SE group:

```yaml
peers: {port: 10000, members: [{name: se-a, address: 10.0.2.11}, {name: se-b, address: 10.0.2.12}]}
```

**Limits.** Cookie, header and URI persistence need HTTP. A cookie value is an opaque hash, never the member's name or
address. Peer names must equal the engines' host names (a deployed group fills this in) and peer addresses must be on a
backend network of the group; with a wrong name that engine falls back to local tables and keeps serving. Cookie
persistence needs no sync, because the client carries the choice.

**Decisions:** [0105](../adr/0105-pool-persistence-kinds.md), [0118](../adr/0118-persistence-peers.md).
