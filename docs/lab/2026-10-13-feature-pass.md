# Feature pass on the two-host Proxmox lab (2026-10-13)

Every feature that reached `main` through the audit work (!141), run on real Proxmox hardware
([runbooks/lab-hosts.md](../runbooks/lab-hosts.md)). The never-lab-tested features came first.

## Setup

- **Controller:** the Compose quickstart in CT 9980, redeployed from `main` (6f2fc5b) and then from the fix branch (!142); the
  database is at the head revision `0037`. The previous engines (older agent) stayed reachable and healthy under the new controller.
- **Engines:** templates 9764 (pve01) and 9774 (pve02) rebuilt from `main` as privileged LXC with the sidecars and `tcpdump`; the
  `edge` group moved to two new LXC engines (`edge-3`, `edge-4`). A KVM template (9766) was built for the autoscaler test and
  removed afterwards.
- **Kubernetes:** the two-node k3s of [2026-10-12-kubernetes.md](2026-10-12-kubernetes.md) and the Helm install of
  [2026-10-12-kubernetes-install.md](2026-10-12-kubernetes-install.md), upgraded to the same code.
- **Helpers:** the helper container (backends, BGP upstream) plus a second container as the client, FreeRADIUS and dnsmasq on the
  helper, OpenLDAP and a Dex instance in Docker on the controller, and small stub servers where a real third party was out of reach
  (Cloudflare API, a CAPTCHA provider). The workstation ran OpenTofu and Chrome. The helper script `deploy/lab/e2e/drive.py` gained
  a generic `raw` command for this (placeholders for object names, base64 bodies, `If-Match`).

## Results

| Feature | Result | What was run |
| --- | --- | --- |
| UDP direct return (`dr`) | **pass** | VS `forward_mode: dr`, member holds the VIP on `lo` with ARP suppressed: 5 of 5 replies, from the VIP, not through the engine |
| UDP tunnel (`tunnel`) | **pass** | IPIP to the member (`tunl0`, VIP on it, `rp_filter` 0): 5 of 5 replies direct. The engine's `UDP_CHECK` goes to the member's own address, so the member must listen there too ([render.md](../render.md)) |
| Automatic scale-out | **pass** | `maxconn` 30, 30 % for 30 s, 24 held connections: decision `busy` (42 %), then a QEMU engine was deployed from the KVM template, the guest agent wrote the enrollment files once it answered, the engine joined and scored like its peers, decision `cooldown` then `idle` |
| Packet capture | **pass** | a UDP capture on an engine finished (`done`, packets and size reported) and downloaded as a valid pcap (magic `d4c3b2a1`) |
| Labels and `?label=` | **pass** | set through a change set on a VS and a pool; `key`, `!key`, `key=value`, `key!=value`, two selectors together; a bad key is 422; the UI filter box and chips agree |
| Tenant quota portal | **pass** | request, duplicate (409), lower or unlimited key (422), approve with a smaller grant (quota changed), deny (note required), withdraw; the UI page lists them |
| External (ESXi) controller member | **pass (API)** | members with a Patroni URL: roles filled from a Patroni stand-in on the helper, `external` set for ESXi, a loopback URL refused. No real ESXi host |
| WAF API schema | **pass** | `syctl waf api-schema` on an OpenAPI file, a `waf` policy set with `api.mode: block`: 15 of 15 requests answered as documented (200, 400, 404, 405, 413, 415) |
| Device posture agent | **pass** | OpenBao `pki-devices`; `sy-posture` enrolled, a compliant report got a 12 h certificate; a policy that needs disk encryption gave `disk_not_encrypted` (exit 3); the certificate was admitted on a client-CA port (200) and a client without it was refused at TLS; the local tunnel carried plain HTTP to the port |
| Cloudflare DNS from a VS | **pass (stub)** | against a stand-in for the Cloudflare API (no token): A records created, TTL and names updated, records deleted when the service is disabled and created again |
| RADIUS login gate | **pass** | real FreeRADIUS 3: password step, a `code` step on a second server, wrong password and wrong code 401, five failures lock (429), `Message-Authenticator` required (the default) works. The gate's session carries the groups |
| Access set with `min_factors` | **renders and applies** | the HAProxy config for a step-up rule is accepted by the engines. The redirect to the gate was not driven |
| LDAP directory (broker) | **pass** | OpenLDAP over LDAPS: the rendered Dex config signs in `alice` (e-mail, groups `ops` and `staff`) and `bob`, a wrong password is 401, through Dex's authorization-code flow and token exchange |
| JWT policy | **pass** | 11 of 11: no token, expired, wrong issuer, wrong audience, bad signature, malformed, missing scope 401; valid 200; a path with a looser rule 200 |
| Client-certificate identities | **pass** | 8 of 8: certificate required for the `valid` rule, identity and group rules (one user in `ops`, one only in `staff`), a self-signed certificate with the same CN refused at TLS |
| CAPTCHA gate | **pass (stub)** | Turnstile settings against a stand-in `siteverify` (trusted certificate, DNS stub on the engines): challenge page, bad token 403, good token 302 and the cookie, the cookie admits, a forged cookie is challenged again, skip path |
| Deny lists | **pass** | a reputation feed and a country list served from the helper; after `sync_ip_lists` the listed client got 403 on both services |
| BGP `max_prefixes` | **renders, does not protect** | `maximum-prefix 2` is on the engines, but an upstream announcing four prefixes did not trip it (see the findings) |
| dnsdist | **pass** | `ANY` REFUSED, AXFR refused, NOTIFY NOTIMP, a burst of 600 queries answered in part (the rest dropped), normal answers right after |
| Kubernetes Gateway status | **pass** | the cluster test lists `patch gateways/status`; the Gateway shows `Accepted` and `Programmed` with the VIP; pool members follow scaling |
| OpenTofu, real binary | **pass** | OpenTofu 1.13.1 with the provider built from `main`: plan, apply (monitor, pool, VS; the VIP served 200), a second plan with no changes, an update, destroy |
| ACME TLS-ALPN-01 | **pass** | issuance against Pebble on the final code (also checked after !126) |
| UI against the lab | **pass after the fixes** | Chrome (Playwright, break-glass with TOTP): 27 pages load; before the fixes the SE groups and Kubernetes pages failed and the header never reached "Stream live" |
| Helm install on `main` | **pass** | the k3s release upgraded to the new images: migration Job complete, all endpoints 200, the VIP still served |

## Bugs found and fixed (!142)

- `GET /tenants/{t}/status/engines` and the engine event stream answered **500** for every engine with load data: the status read
  model did not know `agent.load` (stored by the status poller, read by the autoscaler). The UI's SE groups page failed and the
  live stream stayed on "Connecting".
- `GET /tenants/{t}/kubernetes-clusters` answered **500** once a sync had changed something: `last_sync.last_change` was stored
  (!130) but missing from the response model.
- A RADIUS server could not be given as an IP address (the field used the host-name validator) although ADR 0195 allows a
  routable IP.
- PowerDNS zones got the invalid `a.misconfigured.dns.server.invalid.` SOA name; the controller now creates the zone with an SOA
  whose primary is its first name server.
- `se-image/kvm/build-template.sh` hung for 25 minutes when its web server could not start (port in use); it now stops at once.
- Docs: UDP health checks need the member to listen on its own address, a wrong-type path parameter is a `path` 404, pool members
  in OpenTofu need an `id`.

## Findings that are not fixed

- **BGP `max_prefixes` does not trip.** FRR counts accepted prefixes and the engines' inbound policy accepts none, so a flood of
  prefixes from the upstream is never counted. The field needs a design decision (accept into the BGP table and refuse at the RIB,
  or a different guard); documented as not protecting for now.
- The Lua CAPTCHA call posts the form body with chunked encoding; a provider that refuses chunked requests would fail. Real
  Turnstile and hCaptcha were not reachable from the engines.
- `sync_ip_lists` has no API trigger (the job runs when the worker starts and every six hours); the lab queued the job with SQL.
- Packet capture and autoscaling have no screen in the UI (the SE group's engine menu has Metrics, Enrollment token, Retire,
  Remove record).

## Not run

SAML (no identity provider in the lab); the browser flow from an engine's `oauth2-proxy` through the broker's Dex, and the
step-up redirect to the gate; a real ESXi host, real Cloudflare, Turnstile or hCaptcha; Windows and macOS posture measurements.

## Lab notes

- The `osixia/openldap` image ships a CA that expired in January 2026: use your own certificates for the LDAP test.
- HAProxy's resolvers do not read `/etc/hosts`: the CAPTCHA Lua call needs a DNS answer on the engine.
- The KVM build-template script serves the build files on port 8099 by default (`PORT`); the lab already had a server there.
