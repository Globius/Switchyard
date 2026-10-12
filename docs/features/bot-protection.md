# Bot and DDoS protection

**What.** A `protection` policy set for `http` services: per-source connection limits, slow-client timeouts, user-agent
rules, and either a JavaScript proof-of-work challenge or a CAPTCHA gate (Cloudflare Turnstile or hCaptcha).

**How to use.**

```yaml
kind: protection
name: edge
max_connections_per_source: 50
max_new_connections_per_source: 100
connection_rate_period_s: 10           # 1, 10 or 60
header_timeout_s: 5
client_timeout_s: 30
block_empty_user_agent: true
block_scanners: true
deny_user_agents: [badbot]
bot_action: deny                     # or silent_drop
exempt_cidrs: [198.51.100.0/24]
challenge: {difficulty: 4, valid_for_s: 3600, skip_paths: [/api, /healthz]}
# or captcha: {provider: turnstile, site_key: ..., secret_ref: kv/tenants/acme/captcha, valid_for_s: 3600}
```

The challenge runs natively in HAProxy (no sidecar, no secret). The CAPTCHA runs as fixed Lua rendered by the controller; the
provider secret reaches the engine only in the signed agent bundle, never in a config file or the database.

**Limits.** One protection set per service; `challenge` and `captcha` exclude each other. Connection limits see the
connection source, so behind a CDN or PROXY-protocol balancer use [rate limits](rate-limits.md) instead. A challenge stops
tools that do not run JavaScript, not a real browser driven by a bot. Volumetric DDoS belongs upstream (BGP, the provider),
not on the engines. A CAPTCHA provider outage stops new clients; clients with a valid cookie are unaffected.

**Decisions:** [0170](../adr/0170-bot-and-ddos-protection.md), [0171](../adr/0171-javascript-challenge.md),
[0172](../adr/0172-captcha-gate.md).
