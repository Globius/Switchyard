# CrowdSec

**What.** Engines refuse clients that a CrowdSec security engine has banned, within about ten seconds, and keep doing so when the controller
or the CrowdSec API is down. A CrowdSec policy on a service chooses the mode (log only or block), what happens when CrowdSec does not
answer (open or closed), and which networks and paths are never checked. Services can also send their access logs to the CrowdSec engine,
which detects scans and attacks and turns them into bans every engine enforces.

**How.** The SE image carries the CrowdSec HAProxy SPOA bouncer as a sidecar next to the WAF. The agent starts it from the signed bundle when a
service has a CrowdSec policy; HAProxy asks it per request. The operator connects the CrowdSec engine under Admin > Integrations; the bouncer
key lives in OpenBao. Full guide: [CrowdSec](../crowdsec.md). Operator procedures and the sharing choice: [runbook](../runbooks/crowdsec.md).

**Limits.** Only `ban` decisions on addresses and ranges are enforced (not captcha). Request bodies are not inspected by CrowdSec; use the
[WAF](waf.md). The access log feed uses CrowdSec's UDP syslog datasource, which can lose messages under heavy load. Sharing with CrowdSec is
the operator's explicit choice on the CrowdSec engine and is off in the reference deployment.

**Decisions:** [0213](../adr/0213-crowdsec-bouncer-source.md), [0215](../adr/0215-crowdsec-engine-bouncer-and-signals.md).
