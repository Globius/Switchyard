# GSLB on the two-host lab

Treat each Proxmox host as a **site**. Goal: one wide IP, `shop.gslb.lab.test`, answered from both sites; stop one site's service
and see the answer move, then bring it back. Background: [gslb.md](../gslb.md).

## 1. Two PowerDNS servers

On each host (or a guest on it) run `deploy/dns/` (`PDNS_API_KEY`, `PDNS_API_ALLOW_FROM` = the controller). Call them `pdns-a`
(the one the controller writes to) and `pdns-b`. On the controller set `SWITCHYARD_POWERDNS_URL` to `pdns-a` and its key.

## 2. Zone and secondaries

As system administrator: `POST /tenants/<tenant>/gslb-zones` with `lab-ns-a` and `lab-ns-b` as nameservers (`gslb.lab.test`).
Make `pdns-b` a secondary: create the same zone there as `Slave` with `masters` = `pdns-a`, and allow transfers on `pdns-a`
(`--allow-axfr-ips=<pdns-b>` and `--also-notify=<pdns-b>`, or `allow-axfr-ips` in its config). Check that the LUA records arrive:

```sh
dig +short @pdns-b shop.gslb.lab.test      # empty until the wide IP exists
```

## 3. A wide IP over the two sites

Run the same virtual service in an SE group on each host (VIP of host 1 and VIP of host 2), then:

```sh
syctl api POST /tenants/<t>/gslb-zones/gslb.lab.test/wide-ips '{"name":"shop","algorithm":"failover","ttl":10,
  "health":{"kind":"http","port":80,"path":"/","timeout_ms":500},
  "members":[{"address":"<VIP host 1>","site":"host1","priority":1},{"address":"<VIP host 2>","site":"host2","priority":2}]}'
```

## 4. Prove it

1. `dig +short @pdns-a shop.gslb.lab.test` and `@pdns-b`: both answer the VIP of host 1.
2. Stop the service (disable the virtual service, or stop HAProxy on host 1's engines). Within the check interval plus the TTL,
   both servers answer host 2's VIP. The wide IP's `status` stays `in_sync`: the records did not change, PowerDNS chose differently.
3. Start it again: the answer returns to host 1.
4. `algorithm: all` answers both VIPs while both are up and one when one is down.
5. Edit a record in PowerDNS by hand: `GET .../gslb/status` says `drift`; the job does not fix it; `POST .../gslb/sync` does.
6. Move the virtual service's VIP in the controller (member by `virtual_service`): within five minutes the record follows.
7. `POST .../dnssec`, publish the DS in the parent, and check `dig +dnssec`.

If `pdns-b` stays empty after step 2, transfers are not allowed or the secondary lacks `enable-lua-records=yes`.
