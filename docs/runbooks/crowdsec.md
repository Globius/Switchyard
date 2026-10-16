# Runbook: CrowdSec

How to run the CrowdSec engine Switchyard uses, rotate its key, turn sharing on or off, and check that it all works. The design is
[ADR 0215](../adr/0215-crowdsec-engine-bouncer-and-signals.md); the user guide is [CrowdSec](../crowdsec.md).

## 1. Run the engine

Use `deploy/crowdsec/` (Docker Compose, the official `crowdsecurity/crowdsec` image):

```bash
cd deploy/crowdsec
cp crowdsec.env.example crowdsec.env     # never commit crowdsec.env
$EDITOR crowdsec.env                     # CROWDSEC_BIND (a private address) and CROWDSEC_BOUNCER_KEY
docker compose --env-file crowdsec.env up -d
```

Generate the key with `openssl rand -hex 32`, keep it in OpenBao (`bao kv put kv/system/crowdsec/bouncer-key value=...`; the secret is the field `value`) and give the same value to
Switchyard in **Admin > Integrations**. The Compose file installs the `crowdsecurity/haproxy` collection, listens for syslog on UDP 514 and registers the
bouncer `switchyard`. Publish both ports on a private address only. Back up the `config` and `data` volumes (the engine's credentials and database).

The engine needs `/var/lib/crowdsec/data` on a volume (CrowdSec 1.7 and later refuse to start without it); the Compose file has one.

## 2. Connect Switchyard

**Admin > Integrations > CrowdSec**: local API address (`http://<CROWDSEC_BIND>:8080` on a private network), bouncer key, origins, access log
address (`<CROWDSEC_BIND>:514`). **Test connection** must report the number of banned networks.

Every engine must reach the local API (TCP 8080) and, for the log feed, the syslog port (UDP 514). The bouncer verifies TLS to the API; for HTTPS
use a certificate the engines trust.

## 3. Switch a service on

1. Add a CrowdSec policy in **detect** mode to the service and apply the change set. The apply writes the bouncer configuration to every engine
   and starts the bouncer there.
2. Check an engine: `systemctl status crowdsec-spoa-bouncer` (active), `journalctl -u crowdsec-spoa-bouncer` (it logs each decision update).
3. Ban yourself on purpose from a test address: `docker compose exec crowdsec cscli decisions add --ip 192.0.2.50 --duration 10m`. Within about
   10 seconds the engine logs show `{ban}` for requests from that address. `cscli decisions delete --ip 192.0.2.50` removes it.
4. When the logs look right, switch the policy to **block**.

## 4. Bouncer keys: shared or one per engine

Admin > Integrations > **One key per engine** decides. Off: every engine uses the shared key from step 1. On: every engine uses its own key at
`kv/system/crowdsec/bouncer-keys/<engine>`, so you can rotate or revoke one engine without touching the others. CrowdSec cannot create a
bouncer for a caller, so you register each engine yourself (from `deploy/crowdsec/`, with `BAO_ADDR` and a token that may write `kv/system/crowdsec/`):

```bash
./register-engine.sh add edge-1       # registers the bouncer "edge-1" in CrowdSec, stores its key in OpenBao, prints no key
./register-engine.sh revoke edge-1    # deletes the bouncer and the stored key (engine retired, or a key leaked)
```

An engine whose key is missing fails its apply with the exact command to run. To **rotate** one engine: `revoke`, `add`, then re-apply that engine's SE group
(any change set that touches a protected service). Until it gets the new bundle that engine keeps enforcing the bans it has but cannot refresh them, and the
status poller reports its bundle as drifted; the other engines are not affected.

With the shared key, CrowdSec cannot update an existing bouncer in place: `docker compose exec crowdsec cscli bouncers delete switchyard`, put the new key in
`crowdsec.env` and OpenBao, recreate the container, paste the new key in Switchyard (**Replace**), and re-apply every protected SE group.

## 5. Sharing signals with CrowdSec (off by default)

The reference Compose file sets `DISABLE_ONLINE_API=true`: the engine sends **nothing** to CrowdSec and receives no community blocklist.
`docker compose exec crowdsec cscli capi status` then answers that no Central API is configured.

Before you turn it on, read CrowdSec's EULA (booking.crowdsec.net/crowdsec-eula, v2.0.1) and privacy policy. A plain-fact summary of the EULA is in
[CrowdSec](../crowdsec.md#crowdsecs-terms-plain-facts); it is not legal advice. Enrollment and sharing are done on the CrowdSec engine, not in Switchyard: CrowdSec
has no way to do them remotely, and the Integrations screen shows the commands to copy.

What CrowdSec's documentation says an engine with the online API on sends: signal meta-data (scenario name, hash and version, time, the
engine's machine id, the offending IP with geolocation) and its list of enabled scenarios, plus the names and versions of its engines and
remediation components. Custom and tainted scenarios' alerts are sent by default. Enrolling in the console additionally enables `manual`
(manual decisions) and, per the container documentation, `tainted`, `custom` and `context` (HTTP method, status, path, user agent and target
host of alerts). Only signals from unmodified Hub scenarios count towards the community blocklist. The access logs themselves are not sent:
CrowdSec states that it does not keep raw logs after processing.

To turn it on:

1. Set `CROWDSEC_DISABLE_ONLINE_API=false` in `crowdsec.env` and recreate the container. The engine registers with the Central API.
2. To use the console (dashboards, more blocklists): create an account at app.crowdsec.net, copy the enrollment key (treat it as a credential: keep it in
   OpenBao, put it into `crowdsec.env` only at deploy time) as `CROWDSEC_ENROLL_KEY`, recreate the container, then accept the engine in the web
   app and restart the engine. You can switch parts off afterwards: `cscli console disable context` (and `custom`, `tainted`, `manual`).
3. Tell tenants: services with **Send access logs** feed this engine, so their attackers' addresses can end up in signals. That is the point of the
   feature, and why it is opt-in per service and off by default.

To turn it off again set `CROWDSEC_DISABLE_ONLINE_API=true` and recreate; delete the engine in the console if you no longer want it listed.

## 6. Test plan for a lab

Run before a release or when the SE template changes (the automated version is `tests/integration/test_crowdsec_bouncer_e2e.py`):

| Step | Expect |
| --- | --- |
| Policy in detect mode, ban a client with `cscli decisions add` | Requests succeed; the access log shows `{ban}` within ~10 s |
| Switch to block | The banned client gets 403, others 200 |
| `cscli decisions delete` | The client gets 200 again within ~10 s |
| Stop the bouncer (`systemctl stop crowdsec-spoa-bouncer`) | Fail-open services serve; fail-closed services answer 503 |
| Stop the CrowdSec engine, keep the bouncer running | Banned clients stay blocked; others are served |
| Allowlist the banned client's network in the policy | That client is served |
| Turn on **Send access logs**; run a scan (`for i in $(seq 25); do curl -s -o /dev/null https://site/probe-$i; done`) | After a short delay `cscli alerts list` shows `crowdsecurity/http-probing` and a ban appears; the bouncers block the scanner |
| `cscli metrics` | `Acquisition Metrics` shows lines read and parsed from `syslog:` and the parser `crowdsecurity/haproxy-logs` |

## 7. Troubleshooting

| Symptom | Check |
| --- | --- |
| Apply fails with "CrowdSec is not configured" or "bouncer key was not found" | Admin > Integrations; the reference exists in OpenBao |
| Apply fails with "no log address is set" | A policy has Send access logs on; set the access log address |
| Nothing is blocked | The engine template has the bouncer binary (`/usr/local/bin/crowdsec-spoa-bouncer`); the unit is active; the policy is in block mode; the client address is the real one |
| Everything answers 503 | A fail-closed policy and the bouncer is down or unreachable: `journalctl -u crowdsec-spoa-bouncer` |
| `cscli metrics` shows lines read but none parsed | The header must be RFC 3164 with a host name and program `haproxy`: check the Vector drop-in `/etc/switchyard/vector/crowdsec.yaml` |
| Lines are missing during an attack | The syslog datasource is UDP and can drop above a few hundred events a second |
| Bouncer logs 403 from the API | The key does not match: rotate it (section 4) |
