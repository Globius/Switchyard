# 0143. WAF logs in Loki

Status: Accepted
Date: 2026-10-12

## Decisions

1. Coraza writes its audit lines to the journal of `coraza-spoa.service`. The SE image's Vector configuration reads that unit next to the HAProxy
   access log and ships both to Loki with the same labels (`tenant`, `tenant_name`, `se_group`, `engine`, `frontend`). The `frontend` label of a
   WAF line is `vs_<service>` taken from the application name, so the tenant-scoped log API and the Grafana proxy (ADR 0085) show WAF events
   next to the access log of the same service, and a tenant can never see another tenant's lines.
2. WAF lines start with `waf ` so a query can filter them (`|= "waf "`).
3. Detect mode logs the matches that blocking would act on; that is the data a tenant needs before enabling `block`.
4. A dashboard and alert rule on WAF events belong to the alerting work (P6), which already reads Loki.
