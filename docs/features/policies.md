# Policy sets and request stages

**What.** Request handling beyond routing lives in tenant-owned **policy sets**, shared between services and attached in
`policy_set_ids`. The `kind` decides what a set does: `redirect`, `rewrite` (set, add or remove headers; replace a path
prefix), `respond` (fixed answers, maintenance pages), `error_pages`, `allow_list`, `rate_limit`, `jwt`, `client_cert`,
`access`, `directory`, `waf` and `protection`.

![Policies, with a WAF policy open](../assets/screenshots/policies.png)

**How to use.**

```yaml
kind: rewrite
name: headers
rules:
  - {phase: request, action: {type: set_header, name: X-Client-IP, variable: client_ip}}
  - {phase: response, action: {type: remove_header, name: Server}}
  - {match: {path_prefix: /old}, action: {type: replace_prefix, from_prefix: /old, to_prefix: /new}}
---
kind: respond
name: maintenance
rules: [{match: {}, status: 503, content_type: text/html, body: "<h1>Back soon</h1>", retry_after_s: 600}]
```

**Order.** Stages are fixed by kind, not by attachment order: connection filter, header hygiene, rate limits, protection,
redirects, fixed responses, token and certificate checks, request rewrites, login, routing, response rewrites. Inside a stage,
sets apply in attachment order and rules in list order; in stages that end a request the first match wins.

**Limits.** No free-form HAProxy snippets. Header values come from a fixed alphabet or a fixed list of variables (client IP,
host, TLS version, request id and so on), so a tenant can never write an HAProxy fetch. Response bodies are served as files,
never evaluated. Policy sets apply to `http` services (the allow and deny lists also to `tcp`).

**Decisions:** [0100](../adr/0100-policy-model-and-request-stages.md), [0102](../adr/0102-header-and-url-rewrite.md),
[0103](../adr/0103-fixed-responses-and-error-pages.md).
