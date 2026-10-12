# ADR 0079: ACME providers, DNS credentials and HTTP-01

Status: Accepted

## Context

ACME issuance was DNS-01 through Cloudflare with one directory set by environment variables
(ADR 0010). The operator wants any ACME CA (including private ones and CAs behind OAuth such as
Keyfactor), DNS-01 through other DNS providers, HTTP-01, a choice per certificate, and all of it
configurable from the UI by people who do not edit configuration files.

## Decision

- **ACME providers are system-wide objects** (`acme_provider`), managed by system administrators and
  saved live (not in change sets: they change no engine). A provider holds the directory, contact
  address, the authentication (`none`, `eab`, `oauth`), optional extra trusted roots, and a default
  flag. The account key (generated or pasted), the EAB key and the OAuth client secret are in OpenBao
  under `kv/system/acme/<name>/`. Presets fill the form for the well-known CAs.
- **OAuth** uses the client-credentials grant with a cached token. The token is used to fetch an EAB
  key from a management endpoint (`eab_url`), and optionally sent on every ACME request
  (`bearer_on_acme`).
- **DNS credentials are per tenant** (`dns_provider`, with RLS), managed by tenant administrators.
  Each provider type declares its fields in a catalogue, so the API and the UI use one generic form.
  Secret fields are write-only and stored as one JSON document at `kv/tenants/<tenant>/dns/<name>`.
  Types: Cloudflare, Route 53 (SigV4 without an SDK, optional role assumption), Azure DNS, Google Cloud
  DNS, RFC 2136 with TSIG, PowerDNS. Credentials serve zones (longest match wins, no two credentials
  claim the same zone); a certificate may pin one.
- **Providers append and remove single values** of a TXT set, so `example.test` and
  `*.example.test`, which validate at the same name, do not overwrite each other.
- **Servers named by a tenant** (RFC 2136, PowerDNS) are checked against an address policy, like
  engine addresses (ADR 0072); the other providers talk to fixed public endpoints only (Google's
  token endpoint is fixed, not read from the key file).
- **HTTP-01** is served by HAProxy: every HTTP service with a plain port-80 listener has a first rule
  that answers from a map (`haproxy/maps/acme-http01.map`). The controller adds and removes entries
  through the Data Plane API's runtime map calls and keeps them in the file. All enabled engines of
  the SE groups that serve port 80 must take the entry, because the CA can reach any of them.
- **Certificates carry how they are issued** (`acme_provider`, `challenge`, `dns_provider`), so renewal
  repeats it. Requests are checked before they are queued (`issue/check`), with field paths.
- **Every attempt is recorded** (`certificate_renewal`) for the status column and the history.
- The environment variables of ADR 0010 still work: they become the provider `default` on first use.

## Consequences

- HAProxy configurations of services with a port-80 listener gain one rule and one map file, so
  existing engines show drift until the next apply (a reapply fixes it).
- HTTP-01 cannot work for services that enforce an allow list, and never for wildcard names.
- The controller's own certificate and DNS credential (management tenant) will use these providers in
  a later change.
- Not covered: external account key rollover, ACME profiles, ARI (renewal information), per-tenant
  allow lists of ACME providers (all tenants see all providers).
