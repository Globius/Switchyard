# ADR 0155: The management tenant is created at start-up; an IP can be the public address

Status: Accepted

## Context

The Admin > Controller screen said "there is no management tenant here" on every install that was not
built by an adoption file, even though the tenant is only a name (ADR 0051). The lab also showed an
IPv4 address as "serving now" while the administrator used an IPv6 address, and an IP was refused as
the FQDN.

## Decision

- `syctl serve` creates the locked `management` tenant when it is missing (`ensure_management_tenant`,
  idempotent, safe with several controllers, audited as `system:bootstrap`). No migration: existing
  installs get it on their next start. The tenant is empty and harmless. `syctl adopt` replaces the
  tenant when it is still that empty shell (no SE group, network, service or engine) and refuses a
  tenant that holds anything. The bootstrap audit entry is system-level (no tenant), so replacing the
  shell leaves nothing dangling.
- **Managed mode needs more than the tenant**: a `controller` service with a VIP and engines, which only
  a deployment or an adoption file provides. `ControllerRead.management_tenant` says the tenant exists;
  `managed` stays false until the service does, and the screen should say "the controller service has
  not been set up" rather than "no management tenant".
- `ControllerRead.reached_at` is the address the request came in on. The screen offers it as the public
  address.
- Without a `controller` service the FQDN may be an **IP address** (IPv4 or IPv6; IPv6 is bracketed in
  the URL). With one, the name must be a host name that the certificate covers.
- Confirmation compares the `Host` header address-wise (IPv6 brackets and compression handled).

## Consequences

A fresh controller shows the page as "tenant present, service missing". Nothing is invented: the VIP,
SE group and engines of the controller service still come from a deployment.
