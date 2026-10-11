# Rotate the certificate authority

What is signed by what: the OpenBao PKI (`SWITCHYARD_PKI_MOUNT`, role `SWITCHYARD_PKI_ROLE`) signs the agent
server certificates (ADR 0011). SEs trust it through `ca.crt` in `/etc/switchyard/agent/`, and the controller
uses `SWITCHYARD_DATAPLANE_CA`, `_CERT` and `_KEY` for mTLS to agents and Data Plane APIs. Agent certificates
last seven days and renew themselves, so a rotation is an overlap of two trusted CAs, then a wait.

1. Create the new issuing CA in OpenBao (a new PKI mount, or a new issuer on the same mount) and point the
   role at it. Keep the old issuer enabled.
2. Build a bundle holding both CAs (old then new) and point the controller at it with
   `SWITCHYARD_DATAPLANE_CA`; issue the controller's client certificate from the new issuer. Restart api and
   worker on one node, then the others.
3. Agents receive the CA at enrollment and at each renewal (`sy-agent renew`, timer every 8 hours). Force it
   on one canary SE first: `systemctl start sy-agent-renew.service`. Check `ca.crt` now holds both CAs and
   `GET /tenants/{t}/status/engines` still shows the SE `reachable`.
4. Wait until every engine has renewed (one renewal period plus one certificate lifetime at most). An engine
   whose agent cannot renew needs a new token (`POST …/engines/{id}/enrollment-token`) and `sy-agent enroll`.
5. Only after every engine shows a certificate from the new CA, remove the old CA from the controller
   bundle and from OpenBao.

**Confirm.** `openssl x509 -in /etc/switchyard/agent/tls.crt -noout -issuer` on a few SEs shows the new
issuer, and status shows no unreachable engines.

**Back out.** Before step 5 the old CA is still trusted everywhere: point the role back at the old issuer.

_Rehearsed with `deploy/prod/ca-rotation-drill.sh` against a dev OpenBao: two issuers, a combined bundle that accepts both, then the new CA alone rejects a certificate that was never renewed. That covers the trust logic (steps 1, 2 and 5). The agent renewal in steps 3 and 4 is not rehearsed; it needs a real SE._
