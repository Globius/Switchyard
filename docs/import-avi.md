# Importing an Avi (NSX ALB) configuration

`syctl api import-avi`, `POST /tenants/{t}/imports/avi` and **Tenant > Import** convert an Avi
configuration export into a **draft change set**. Nothing is applied: you review the draft, change what
you want and apply it like any other change set. ADR 0150.

Get the export from the Avi controller (`POST /api/configuration/export`, or the `avi_config.json` inside a
backup). It contains certificate keys: keep it out of Git and out of tickets (`CLAUDE.md`).

```text
syctl api import-avi avi_config.json -t acme --se-group edge --vip-pool main            # report only
syctl api import-avi avi_config.json -t acme --se-group edge --vip-pool main --commit   # open the draft
```

## What becomes what

| Avi | Switchyard |
| --- | --- |
| Virtual service (HTTP application profile) | HTTP virtual service; ports from `services`, `enable_ssl` becomes a TLS port, HTTP/2 from the profile, TLS profile from the SSL profile (`intermediate`, `modern`) |
| Virtual service (L4 profile) | TCP service; UDP network profiles become UDP (IPVS) services; port ranges up to 16 ports are expanded |
| SNI parent and children, enhanced virtual hosting | One HTTP service with a host route per child domain; the children's certificates join the parent |
| Pool, pool group | Pool: algorithm (least load and fastest response become least connections, with a warning), servers (ratios become proportional weights, disabled servers stay disabled), TLS to the servers. A pool group uses its first pool and says so |
| Health monitor | TCP, HTTP (request line, expected 2xx or 3xx) and UDP monitors; ICMP becomes a TCP check, with a warning; identical monitors are shared |
| Persistence | Client IP becomes `source_ip` with its timeout; other kinds are listed to be set on the new pool |
| HTTP policy set | Path and host redirects, path and host pool switches; an unconditional HTTP-to-HTTPS redirect becomes `https_redirect_port` |
| SSL key and certificate | Imported through the certificate service: keys go to OpenBao and never into the change set. Expired certificates and certificates without a key are listed, not converted |
| VIP (VsVip) | **Not carried over.** Each Avi VIP gets one new address from `--vip-pool`, or from `--vip OLD=NEW` (by Avi VIP name or old address); services without an address are left out |

Everything else is reported, never silently dropped. `GET`/`POST` answers carry a list of
**findings**: `info` (converted and how), `warning` (converted approximately), `manual` (someone has to
do it: WAF, DataScripts, bot and security policies, external monitors, other policy actions) and
`skipped` (left behind on purpose: nothing uses it, expired, a server without an address).

Names are lowercased and made valid; clashes with existing objects get `-2`, `-3` ... (`--prefix` puts a
prefix in front). Use `--only NAME` to convert a few services at a time and `--include-unused` to also
convert objects no service uses.

## Before you apply

1. Read the `manual` and `warning` findings; recreate what was not converted (`docs/render.md` lists what
   the policies can do).
2. Check the draft's diff and the rendered configuration of each service.
3. Test each service on its new VIP before moving DNS or Cloudflare to it (design 01, migration plan).
