# 0120. Alerting: targets, rules, and a controller-side evaluator

Status: Accepted
Date: 2026-10-12

## Context
Operators asked to hear about problems without watching the UI: a certificate about to expire, a failed apply, a service
that is slow. The parity plan (B2, C7) wants outbound webhook, email and chat targets per tenant and rules on events and
metrics, with certificate expiry notifications that the ACME work left for this block. Tenants are separate: one tenant's
contacts, rules and history must never be visible to another.

## Decision
- **One tenant-scoped contact model.** `alert_target` (webhook, chat or email) and `alert_rule` (what to watch, which
  targets to tell) are tenant-owned tables with RLS like every other. Both apply live (no change set) and are audited
  (`alert_target.*`, `alert_rule.*`). Tenant administrators write; any role in the tenant reads.
- **The controller evaluates, not Alertmanager.** A scheduled job `evaluate_alerts` (every minute, leader-scheduled like
  `poll_status`) goes through each tenant's enabled rules. No new service to run, and a rule can use what only the
  controller knows (audit events, certificates, the tenant's services and engines). Alertmanager can still be fed from
  Prometheus rules for infrastructure alerts (runbooks/telemetry.md); these are the tenant's alerts.
- **Three kinds of rule**, each with a strict validated `spec` (no free-form expressions):
  - `event`: an audit event with one of the named actions (any tenant-visible action; sign-in and identity-provider events
    are excluded) was written since the rule last looked. One message per event, at most 25 per run.
  - `metric`: a built-in series (`vs_5xx_ratio`, `vs_p95_ms`, `vs_rps`, `engine_cpu_percent`, `engine_memory_percent`,
    `engine_sessions`) is over (`gt`) or under (`lt`) a threshold for `for_minutes`, per service or engine (or one named
    by `scope`). The values come from the same tenant-scoped telemetry client the UI uses, so a rule can never read
    another tenant. The state machine is `ok` → `pending` → `firing` → `ok`; a resolved message follows a firing one.
    Missing data changes nothing (it is neither a breach nor a recovery).
  - `cert_expiry`: days thresholds (default 30, 14, 7). One message when a certificate first crosses a tier and another
    when it crosses the next, one when it has expired, and a resolved message when it is renewed.
- **State and delivery are separate.** `alert_state` (per rule and key) records what is firing; `alert_delivery` records
  each attempt (kept 30 days). State changes whether or not a message got through, so a dead webhook cannot make the
  evaluator resend forever; a failed delivery is visible in `GET /tenants/{t}/alerts`, with the reason.
- **Defaults** (change them with a new ADR): evaluation every 60 seconds; `for_minutes` 5; at most 10 targets per rule;
  delivery history 30 days; an alert fires once per tier or breach, no repeat reminders.

## Consequences
- Delivery is at-most-once per state change: if the controller dies between deciding and sending, that message is lost
  (the state was committed first). Re-notification is deferred until someone needs it.
- Evaluation runs on whichever node holds scheduler leadership and one worker takes the job, so there is one evaluator
  cluster-wide. A tenant with many services costs one summary query per metric rule per minute.
- Rules on arbitrary PromQL are not offered. Add metrics to the fixed list when a need is clear.
- There is no maintenance-window or silence feature; disable the rule.

## Alternatives considered
Alertmanager per tenant or with tenant routing (two more services, and the audit and certificate data would need
exporters); a rules language (injection and cost surface); evaluating inside Prometheus (cannot see audit events or
certificates).
