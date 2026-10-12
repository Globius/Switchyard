# Alerting

Tell people when something needs them, without watching the UI. Decisions: [ADR 0120](adr/0120-alerting-model.md) (model),
[0121](adr/0121-alert-senders-and-destinations.md) (senders), [0122](adr/0122-certificate-expiry-notifications.md)
(certificates), [0123](adr/0123-tenant-log-api.md) (tenant log).

Everything is per tenant. Tenant administrators create targets and rules; every role in the tenant can read them and
see what is firing. Changes apply at once (no change set) and are audited as `alert_target.*` and `alert_rule.*`.

## Targets: where alerts go

| Kind | `config` | Notes |
| --- | --- | --- |
| `webhook` | `{"url": "https://..."}` | JSON body (below). Optional `secret_ref` (`kv/...` in OpenBao) signs it |
| `chat` | `{"url": "https://...", "format": "slack"}` | `slack`, `teams` or `mattermost` incoming-webhook URL |
| `email` | `{"to": ["ops@example.com"]}` | 1 to 10 addresses; needs `SWITCHYARD_SMTP_HOST` on the controller |

URLs must be https and must not name a private address (loopback, 10/8, 172.16/12, 192.168/16, link-local, the cloud
metadata address); the name is resolved again at every send. A lab can allow http and private addresses with
`SWITCHYARD_ALERT_ALLOW_HTTP=1` and `SWITCHYARD_ALERT_ALLOW_PRIVATE=1`.

`POST /tenants/{t}/alert-targets/{name}/test` sends one clearly marked test message and tells you whether it arrived.

**Webhook body**

```json
{"version": 1, "tenant": "acme", "rule": "certs-expiring", "state": "firing",
 "key": "cert:shop-example", "title": "Certificate shop-example expires in 13 day(s)",
 "text": "...", "at": "2026-10-12T08:00:00+00:00", "details": {"days_left": 13.2, "tier": 14}}
```

`state` is `firing`, `resolved` or `test`. With a secret the request carries `X-Switchyard-Signature: sha256=<hex>`: the
HMAC-SHA256 of the raw body. Verify it before trusting the call. A receiver must answer 2xx; anything else is recorded as a
failed delivery (visible in `GET /tenants/{t}/alerts`) and not retried.

## Rules: what to watch

| Kind | `spec` | Fires when |
| --- | --- | --- |
| `event` | `{"actions": ["change_set.failed", "drift.detected"]}` | an audit event with one of these actions is written (see `GET /audit-actions`; sign-in events are not offered) |
| `metric` | `{"metric": "vs_5xx_ratio", "op": "gt", "threshold": 5, "for_minutes": 5, "scope": "web"}` | the series is over (`gt`) or under (`lt`) the threshold for `for_minutes` (default 5); `scope` limits it to one service or engine |
| `cert_expiry` | `{"days": [30, 14, 7]}` | a server certificate is within a tier; once per tier, once when expired, a resolved message when renewed |

Metrics: `vs_5xx_ratio` (%), `vs_p95_ms`, `vs_rps`, `engine_cpu_percent`, `engine_memory_percent`, `engine_sessions`
(`GET /alert-catalog` lists them with units). Metric rules need the metrics backend ([runbooks/telemetry.md](runbooks/telemetry.md));
without it they are skipped. A metric rule sends one message when it starts firing and one when it recovers; no data changes
nothing. A rule names 1 to 10 targets of the same tenant; a target that is disabled or deleted is skipped.

The evaluator runs every minute (job `evaluate_alerts`). A new event rule only reports events written after it was created.

## Example

```sh
syctl api POST /tenants/acme/alert-targets '{"name":"ops-chat","kind":"chat","config":{"url":"https://hooks.slack.com/services/T000/B000/XXXX"}}'
syctl api POST /tenants/acme/alert-targets/ops-chat/test
syctl api POST /tenants/acme/alert-rules '{"name":"certs","kind":"cert_expiry","spec":{},"targets":["ops-chat"]}'
syctl api POST /tenants/acme/alert-rules '{"name":"errors","kind":"metric","spec":{"metric":"vs_5xx_ratio","op":"gt","threshold":5,"for_minutes":5},"targets":["ops-chat"]}'
syctl api GET /tenants/acme/alerts
```

## When something is not arriving

1. `GET /tenants/{t}/alerts`: `firing` shows what the evaluator thinks is wrong (a metric rule still waiting out `for_minutes` is `pending`, with the current value and `fires_at`, the time it fires if the value stays past the threshold); `deliveries` shows each attempt with its
   error ("the receiver answered HTTP 403", "the URL names a private or local address", "this controller has no mail server
   configured").
2. A worker must be running: the job `evaluate_alerts` appears in the jobs list every minute.
3. Metric rules say nothing while Prometheus is unreachable (the controller logs `metric rule skipped`).
4. Test the target first; a target that fails its test fails for alerts too.

## Tenant request log

`GET /tenants/{t}/logs` is the request log of the whole tenant, newest first (`service`, `since`, `until`, `q`,
`status_class`, `limit`, `cursor`), from Loki with the tenant label enforced. It is what to open when an alert says a service is
returning 5xx.
