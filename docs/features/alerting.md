# Alerting

**What.** Tenant alert rules evaluated by the controller every minute: `event` (an audit action happened), `metric` (5xx
ratio, p95 latency, request rate, engine CPU, memory or sessions over or under a threshold for some minutes) and `cert_expiry`
(30, 14 and 7 days by default). Messages go to webhook (HMAC-signed), chat (Slack, Teams, Mattermost) or email targets.

**How to use.** Create a target, test it (`POST .../alert-targets/{name}/test`), then a rule naming it. Both change live and
are audited. `GET .../alerts` shows what is firing and each delivery attempt with its result. The full guide:
[Alerting](../alerting.md).

**Limits.** No repeat reminders: one message per tier or breach and one when it resolves. Target URLs must be public https
(lab overrides exist). Email needs the operator's SMTP settings. Infrastructure alerts (stale poll, failed change sets, down
exporters) are Prometheus rules in the telemetry stack, not tenant rules.

**Decisions:** [0120](../adr/0120-alerting-model.md), [0121](../adr/0121-alert-senders-and-destinations.md),
[0122](../adr/0122-certificate-expiry-notifications.md).
