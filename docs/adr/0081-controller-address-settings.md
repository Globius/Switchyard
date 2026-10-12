# ADR 0081: Changing the controller's address, VIP and certificate

Status: Accepted

## Context

Administrators want to manage the controller's FQDN, VIP and TLS certificate from the UI. A wrong value
locks them out of the very screen that would fix it. In production the controller sits behind its own
platform: the `controller` virtual service of the locked `management` tenant (ADR 0051), with TLS ended
by the management SE group (ADR 0053).

## Decision

- The change is a **transaction on that service**, not a new listener inside the API process:
  propose, wait, confirm or revert. A proposal is validated first (FQDN, certificate that covers it and
  is not about to expire, VIP the group can use), then applied as an ordinary change set of the management
  tenant (canary, rollout, recorded reason, audit).
- **Confirmation proves reachability.** It is accepted only on a request that arrived on the new FQDN.
- **The way back does not depend on the new address.** The worker reverts an unconfirmed change when the
  window (default 2 minutes, 30 to 600 seconds) runs out; it reaches the engines through their
  management addresses. The revert restores only the VIP and certificate the change replaced, so later
  edits survive. `syctl controller revert` does the same with database access alone.
- **The public URL** (login redirects, cookies, SAML ids) is a stored setting that changes only at
  confirm. While a change waits, requests that arrive on the pending host already use the pending URL so
  sign-in works there. The identity providers' new redirect addresses are shown before the change.
- Without a management tenant only the public URL can change.
- The settings are one row (`controller_settings`) holding the public URL, the pending change and the
  last result.

## Consequences

- A proposal needs a TLS port on the `controller` service; a service that is plain TCP must be given one
  in the management tenant first.
- A VIP change moves the controller to another address behind DNS you control; confirm works only once
  the new name reaches it.
- Not covered: changing the SE group, the ports or the pool members of the service (ordinary change sets),
  and certificates for the controller itself from ACME in one step (request the certificate first).
