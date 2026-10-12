# WAF

**What.** A web application firewall on `http` services: Coraza with a pinned OWASP Core Rule Set, run as a sidecar on every
engine (`coraza-spoa`) and called by HAProxy for each request.

![Editing a WAF policy](../assets/screenshots/policies.png)

**How to use.**

```yaml
kind: waf
name: shop-waf
mode: detect             # start here; switch to block when the logs are clean
crs_version: "4.8.0"
paranoia_level: 1
inbound_threshold: 5
disabled_rule_ids: [920350]
excluded_paths: [/healthz]
inspect_body: true
fail_mode: open          # or closed: answer 503 when the sidecar does not answer
timeout_ms: 100
```

Attach it in `policy_set_ids`. In `detect` mode every match is logged and nothing is blocked: read the WAF lines in the
service's logs (`|= "waf "`), disable the rules that fire on legitimate traffic, then set `mode: block`.
`GET /waf/crs-rules` lists the rule families and the rules most often disabled.

**Limits.** One WAF policy per service. No custom rules or free-form directives. No response-body inspection. The CRS
release is one of a fixed list shipped in the engine image, so an image update never changes a policy's behaviour. An engine
whose sidecar is down loses 25 points of health score ("WAF sidecar not running"), so a fail-open WAF that inspects nothing
shows up.

**Decisions:** [0140](../adr/0140-waf-policy-set.md), [0141](../adr/0141-waf-detect-first-and-crs-pinning.md),
[0142](../adr/0142-waf-sidecar-and-failure-mode.md), [0143](../adr/0143-waf-logs.md),
[0144](../adr/0144-waf-image-and-agent-contract.md), [0152](../adr/0152-crs-rule-catalogue.md).
