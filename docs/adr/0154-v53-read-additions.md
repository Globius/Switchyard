# ADR 0154: Read additions for the v53 screens

Status: Accepted

- **WAF flag on log lines.** In block mode HAProxy captures the WAF verdict, which the access log line
  shows as `{deny}` or `{drop}` before the request. `AccessLogEntry` gains `waf` and `waf_action`. Detect
  mode has no verdict on the line, so it is never flagged there; the Coraza audit log (ADR 0143) holds
  those matches.
- **Kubernetes test.** When the Gateway API is installed the test also checks `list gateways` and
  `list httproutes`; a missing one fails the test.
- **GSLB latency.** `WideIpRead.members[].latency_ms` (smoothed connect time, null when unknown) and
  `latency_order` (member indexes, fastest first) expose what the prober measured.
