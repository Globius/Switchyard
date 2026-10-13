# 0196. Device posture from an agent, short-lived device certificates, and a TCP tunnel

Status: Accepted
Date: 2026-10-12

## Context

[0138](0138-device-posture-signals.md) used the signals that already travel with a request (a client certificate, claims in the ID
token) and stated what was **not built**: agent-based posture (disk encryption, patch level) and non-HTTP applications, "a client and a
posture service, a separate product decision" (plan items E8 and E9). The audit lists both as missing. A load balancer cannot see a
laptop's disk or patches, so something on the laptop has to say, and what it says has to turn into something the engines can check
without calling the controller (the data plane never depends on the controller, CLAUDE.md).

## Decision

**A device agent, `sy-posture`** (Go, standard library only, `posture-agent/`). It runs on Windows, macOS and Linux and does five things:

| Command | What it does |
| --- | --- |
| `enroll` | trades a one-time token for a device identity; makes an Ed25519 key (signs reports) and a P-256 key (the certificate key, never sent) |
| `report` | measures the device, sends a signed report with a certificate request, stores the certificate it gets back; exit code 3 when not compliant |
| `run` | `report` on the controller's schedule (a third of the certificate's life, at least a minute; back-off on errors) |
| `status` | identity and certificate expiry |
| `tunnel` | a local TCP listener (loopback only) that carries each connection over TLS with the device certificate to a service behind an engine |

Facts (`os`, `os_version`, `disk_encrypted`, `firewall_enabled`, `screen_lock`, `days_since_update`) come from the system's own tools
(`lsblk`, `nft`/`ufw`/`firewall-cmd`; `manage-bde`, `netsh`, `reg`, PowerShell; `fdesetup`, `socketfilterfw`, `sysadminctl`,
`softwareupdate`), run without a shell with fixed arguments, and read by pure functions that are tested with real sample output. A fact
that cannot be established is `null`, and a requirement that needs it fails. BitLocker queries need an elevated process.

**A `posture` policy set** (`requirements`, `pki_role`, `cert_ttl_h`), not attached to services. Requirements: allowed operating systems,
minimum version per system, disk encryption (default on), firewall (default on), screen lock (default off), maximum days since the last
update (default 45). `cert_ttl_h` is 1 to 72 hours (default 12). `pki_role` names the OpenBao PKI role that signs this policy's
certificates and must start with the tenant's name and a dash (`pki_role_not_allowed`): one role is one CA, and a CA shared between
tenants would make a device of one tenant acceptable on another's ports.

**Enrollment and reports.** A tenant administrator creates a token (`POST /tenants/{t}/device-enrollments`, 24 hours, single use, stored
as SHA-256). `POST /device/enroll` trades it and the device's Ed25519 public key for a device id. Every `POST /device/report` carries
the device id, a millisecond timestamp and an Ed25519 signature over `METHOD`, `PATH`, `TIMESTAMP` and `SHA256(body)` joined by newlines.
The controller refuses a timestamp that is not newer than the last accepted one or is more than five minutes off, so a captured report
cannot be replayed, and it answers every credential problem with the same 401. A test vector
(`posture-agent/internal/protocol/testdata/vector.json`) is verified by the agent and by the controller's tests. The path that is
signed is the one the controller sees, so a proxy in front of it must not rewrite the path.

**Certificates.** When the facts meet the policy and the report carries a CSR, the controller signs a **client** certificate through the
policy's PKI role: common name is the device name, one URI SAN `urn:switchyard:device:<tenant>:<id>`, key usage client authentication,
lifetime `cert_ttl_h`. Names come from the device record, never from the CSR. A device that stops being compliant, or is revoked
(`POST /tenants/{t}/devices/{id}/revoke`), gets no new certificate; the one it holds expires within `cert_ttl_h`. The engine needs no
new feature: an HTTP or TCP service with a TLS port whose `client_ca_id` is the role's CA admits exactly the devices holding a valid
certificate (ADR 0070), and `posture.device_certificate` (ADR 0138) can additionally be required per path. Pairing the device with
the person stays with `client_cert` identity (ADR 0136) and the login.

**ZTNA for non-HTTP applications.** A service on a TLS port with `client_ca_id` terminates TLS and requires the device certificate, then
forwards to its pool (a `tcp` service forwards raw TCP). `sy-posture tunnel --listen 127.0.0.1:2222 --to engine:8443` is the client
half: SSH, RDP or a database client connects to the loopback address; the tunnel reads the current certificate for every connection, so
renewals need no restart, and it refuses to listen on a non-loopback address.

**Auditing and access.** `device.enrolled`, `device.enrollment_created`, `device.compliant`, `device.non_compliant` (only when the verdict
changes), `device.revoked`, `device.removed`. Tokens and revocation need the new permission `manage device` (tenant-admin); listing needs
`read tenant_data`. Devices and tokens are tenant tables with forced row-level security (migration 0036); the policy kind is migration 0037.

**Defaults** (chosen without asking): 12 hour certificates; screen lock not required; a report every third of the certificate life; one
certificate per report (the previous one stays valid until it expires); Ed25519 for reports, P-256 for the certificate key.

## Consequences

- **The facts are claims by the device.** An administrator of the machine, or malware running as one, can make the agent say anything
  (the agent runs with the user's rights; it does not attest hardware). The design limits the damage: the result is a short-lived,
  tenant-specific certificate, revocation takes effect within `cert_ttl_h`, and the signal is meant to catch unmanaged, unpatched and
  unencrypted laptops, not a determined attacker on a managed one. Hardware attestation (TPM, Secure Enclave) is not built.
- The controller is needed to *renew*, not to *use* a certificate: engines keep admitting devices while it is down, until certificates expire.
- No CRL is published for device certificates. Revocation is expiry, so keep `cert_ttl_h` short for sensitive services.
- OpenBao needs a PKI mount (default `pki-devices`, `SWITCHYARD_DEVICE_PKI_MOUNT`) with one role per tenant (client flag on, server
  flag off, `allowed_uri_sans` for the URN) and its issuing CA stored as a `client_ca` certificate record. Without OpenBao devices can
  enroll and report but receive no certificate.
- Verified end to end in `tests/integration/test_posture_agent_e2e.py`: the Go agent against the real API on PostgreSQL, a rendered
  HAProxy TLS port that requires the device CA (admits the tunnel, refuses no certificate and a certificate from another CA), a
  non-compliant policy that gets the verdict and no certificate, and a revoked device that is refused. Not tested here: the Windows
  and macOS measurements on real machines (their parsers are tested with sample output, the commands are not run in CI), and the
  OpenBao PKI role (the tests use the in-process CA).

## Not built

Hardware attestation; MDM integration (Intune, Jamf) as a fact source; a packaged installer or system service for the agent (run it
from a scheduler or a service manager; `run` is a plain loop); a CRL or OCSP responder for device certificates; a UI for devices (API
and the agent's CLI only); per-device exceptions.

## Alternatives considered

- **Posture as a header sent by the agent to the engine.** The engine would trust a value the device chose.
- **Posture claims in the identity provider only.** Already possible (0138) and still the way to use Intune or Okta device assurance;
  it does not help tenants without such a provider, and it never covers non-HTTP services.
- **A permanent device certificate plus a compliance list pushed to engines.** Every verdict change would be a configuration
  apply through the canary, and the data plane would need an update path for it.
