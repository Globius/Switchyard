# 0121. Alert senders: what a tenant may point the controller at

Status: Accepted
Date: 2026-10-12

## Context
A webhook URL is tenant input, and the controller sits on the management network next to Proxmox, OpenBao and the
database. A tenant who can make the controller POST anywhere can probe or hit internal services (server-side request
forgery), including the cloud metadata address. Signing secrets must not be stored in PostgreSQL.

## Decision
- **Targets**: `webhook` (JSON body), `chat` (Slack, Teams or Mattermost incoming webhook formats) and `email` (a list of
  addresses, through the operator's SMTP server).
- **URLs are validated twice.** When the target is saved: https only (`SWITCHYARD_ALERT_ALLOW_HTTP=1` allows http for labs),
  a host, no credentials in the URL, no private, loopback, link-local, multicast or otherwise non-global IP literal, no
  `localhost`. At every send: the host is resolved and refused if any answer is a non-global address, which also catches a
  public name that points inside. Redirects are not followed. `SWITCHYARD_ALERT_ALLOW_PRIVATE=1` switches both checks off
  for labs and for operators who really run their receiver on a private network.
- **Signing**: a webhook may carry a `secret_ref` (a `kv/...` path in OpenBao, never the secret); each body is signed with
  HMAC-SHA256 in `X-Switchyard-Signature: sha256=<hex>`, and `X-Switchyard-Event` carries `firing`, `resolved` or `test`.
  The body is `{version: 1, tenant, rule, state, key, title, text, at, details}`; `version` changes only with an
  incompatible change.
- **Errors are short and safe**: the tenant sees "the receiver answered HTTP 500" or "request failed: ConnectError",
  never the URL (chat URLs contain a secret) or a stack trace.
- **Email** is available when `SWITCHYARD_SMTP_HOST` is set (`_PORT`, `_USER`, `_PASSWORD`, `_FROM`, `_STARTTLS`);
  `GET /alert-catalog` says whether it is.

## Consequences
The host is resolved once per send, every answer is checked, and the request connects to the first checked address with
`Host` and the TLS server name kept as the original host (certificate verification is unchanged), so DNS rebinding
between check and connection cannot reach a private address; redirects are never followed. A test rebinds the name and
fails if the client connects by name. Lab mode (`SWITCHYARD_ALERT_ALLOW_PRIVATE=1`) skips all of it. Chat webhook URLs are
stored in `alert_target.config` in plain text (they are bearer URLs); they are shown to tenant administrators and viewers
of the tenant, so treat a chat target as readable by everyone in the tenant, and use a signed webhook when that is not
acceptable.

## Alternatives considered
An allow-list of destination domains per tenant (operational burden); proxying through an egress service (the right answer
for hardened sites, not for a first version); storing chat URLs as secret references (would need OpenBao write access from
the alert API, which the tenant roles do not have).
