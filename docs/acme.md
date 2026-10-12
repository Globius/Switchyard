# Certificates from ACME providers

Switchyard orders certificates from any ACME certificate authority (Let's Encrypt, ZeroSSL, Google
Trust Services, Buypass, Keyfactor, step-ca and others), validates names with **DNS-01** or
**HTTP-01**, and renews them before they expire. Everything is set up in the web UI or the API; no
configuration files are involved. The decision is in ADR 0080.

Prerequisite: the controller needs OpenBao (the account keys, EAB keys, OAuth client secrets and DNS
credentials are stored there; PostgreSQL keeps only references).

## ACME providers (system administrators)

A provider is a certificate authority and the account the controller uses with it. Providers are
shared by all tenants. Register them under **Admin > ACME providers**, with `POST /acme-providers` or
`syctl api acme add`.

| Preset | Directory | Authentication |
| --- | --- | --- |
| `letsencrypt`, `letsencrypt_staging` | Let's Encrypt production / staging | none |
| `zerossl` | ZeroSSL | EAB (from the ZeroSSL dashboard) |
| `google` | Google Trust Services | EAB (`gcloud publicca external-account-keys create`) |
| `buypass` | Buypass Go SSL | none |
| `keyfactor` | your Keyfactor ACME directory | OAuth (below) |
| `custom` | any RFC 8555 directory (step-ca, Pebble ...) | none, EAB or OAuth |

- The controller creates the **account key** itself (RSA 2048) and keeps it in OpenBao. To bring an
  existing account, paste its key (`account_key`, write-only).
- **External Account Binding**: give `eab_kid` and `eab_hmac_key` (write-only).
- **OAuth** for a CA behind an identity provider: `oauth.token_url`, `client_id`,
  `oauth_client_secret` (write-only), optional `scope` and `audience`. The controller signs in with
  the client-credentials grant, caches and refreshes the token, and uses it in two ways: `eab_url`
  names a management endpoint that returns a fresh EAB key (JSON with a key id and an HMAC key) when
  called with the bearer token, and `bearer_on_acme` sends the token on every ACME request. At least
  one of them is required. Keyfactor's token flow is described in its ACME authentication guide.
- **Private CAs**: `ca_bundle_pem` adds trusted root certificates for the connection to the CA.
- **Test connection** (`POST /acme-providers/{id}/test`) checks the directory, then the EAB
  credentials, then registers the account. Nothing is ordered. A success sets `account_registered`.
- One provider can be the **default**: it is used when a certificate names none. Providers that
  certificates use cannot be deleted (`used_by` counts them).

## DNS credentials (tenant administrators)

DNS-01 publishes `_acme-challenge` TXT records through a DNS provider. Each tenant keeps its own
credentials under **Tenant > DNS credentials** or `POST /tenants/{t}/dns-providers`.
`GET /dns-provider-types` lists the types and the fields of each form; fields marked `secret` are
write-only and stored in OpenBao.

| Type | Fields (secret in italics) |
| --- | --- |
| `cloudflare` | *api_token* (Zone:Read, DNS:Edit) |
| `route53` | *access_key_id*, *secret_access_key*, *session_token*, hosted_zone_id, role_arn |
| `azure` | azure_tenant_id, client_id, *client_secret*, subscription_id, resource_group, zone, cloud |
| `google` | *service_account_json*, project_id, managed_zone, zone |
| `rfc2136` | server, port, zone, key_name, algorithm, *tsig_secret* |
| `powerdns` | api_url, server_id, zone, *api_key* |

- **Zones** say which names a credential serves (`example.test` also covers `*.example.test`). The
  longest matching zone wins; a credential without zones serves names nobody else claims. Two
  credentials of a tenant cannot claim the same zone. A certificate can also name its credential.
- **Test** publishes and removes `_switchyard-test.<zone>` in every zone and reports per zone.
- Names that share a challenge record (`example.test` and `*.example.test`) work with every
  provider: only the controller's own value is added and removed.
- Servers you name (RFC 2136, PowerDNS) may not be loopback, link-local or multicast addresses;
  private addresses need the operator to allow the network with `SWITCHYARD_DNS_CIDRS`
  (comma-separated; `SWITCHYARD_ENGINE_POLICY=open` lifts the rule in labs).

## Requesting a certificate

**Certificates > Request certificate**, `POST /tenants/{t}/certificates/issue` or
`syctl api cert issue`. Choose the provider (default if empty), the names, the validation method (`dns-01`, `http-01`, `tls-alpn-01`) and
the renewal (`auto`, `renew_before_days`). `POST /tenants/{t}/certificates/issue/check` runs the same
checks without queueing and returns every finding with its field path:

- the provider exists and is enabled;
- **DNS-01**: every name has a credential that covers it (`GET /tenants/{t}/dns-providers/match?names=`
  lists the candidates per name, longest zone first);
- **HTTP-01**: one name, no wildcard, and it resolves to a VIP of one of your HTTP services with a
  port-80 listener that does not enforce an allow list;
- **TLS-ALPN-01**: no wildcard, and each name resolves to a VIP of one of your HTTP services with a
  TLS listener on port 443 (names that already serve HTTPS give a warning, not an error).

The job reports the stages `order`, `validate`, `finalize`, `store` and `change set`. The result is a
`system:certs` change set, validated, audited, canaried and revertible like any other. A failure
quotes the CA's error, and the log says whether the challenge records were removed.

### HTTP-01

Every HTTP service with a plain port-80 listener answers `/.well-known/acme-challenge/<token>` from
an (initially empty) map, `haproxy/maps/acme-http01.map`, before redirects and routes. While a
certificate is validated the controller adds the answer to the map of every enabled engine behind
the VIP through the Data Plane API's runtime calls (no reload) and removes it afterwards. If an
engine cannot take it, the order is not started. An engine that still runs a configuration from
before this feature reports `apply any change set to its SE group first`: applying any change
re-renders the rule.

### TLS-ALPN-01

For deployments where only port 443 is reachable (ADR 0082). Every HTTP service with a TLS port renders the
ALPN protocol `acme-tls/1` next to `h2` and `http/1.1`. While a certificate is validated the controller
loads the RFC 8737 challenge certificate into each engine's HAProxy through the Data Plane API (no reload)
and adds it to the crt-lists of the group's TLS services, for that name and for clients that offer
`acme-tls/1`, and removes it afterwards. No wildcard names. The name has to lead to a VIP of an HTTP service
with a TLS listener on port 443.

**Visitors of that name see a certificate error for the few seconds validation takes**, because HAProxy picks the
certificate by name, not by ALPN. `issue/check` returns a `warnings` entry for names that already serve HTTPS.
Choose DNS-01 or HTTP-01 where that matters.

### Renewal

A daily job renews `auto` certificates `renew_before_days` (default 30) before they expire, with the
settings stored on the certificate. Every attempt is recorded:
`GET /tenants/{t}/certificate-renewals` shows issuer, expiry, when renewal starts and the last
result; `GET /tenants/{t}/certificates/{id}/renewals` is the history with the CA's error. "Retry
renewal now" is `POST /tenants/{t}/certificates/{id}/renew`.

## Earlier configuration

`SWITCHYARD_ACME_DIRECTORY`, `_EMAIL` and `_ACCOUNT_KEY_FILE` still work: on first use they become the
provider `default`. `SWITCHYARD_CLOUDFLARE_TOKEN` still serves DNS-01 for tenants that have no DNS
credential of their own (and Origin CA and the IP-range sync). Create providers in the UI instead;
`SWITCHYARD_ACME_DNS_WAIT` (seconds, default 15) sets the pause after publishing DNS records.

## Permissions

| Action | Role |
| --- | --- |
| Read providers, DNS credentials (no secrets), renewal history | any signed-in user / viewer |
| Request or renew a certificate | `operator` |
| Manage DNS credentials | `tenant-admin` |
| Manage ACME providers | `system-admin` |
