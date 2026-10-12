# Device posture and device certificates

Some services should only be reachable from laptops you manage: disk encrypted, firewall on, patched. Switchyard does this in two
steps (ADR 0196). A small agent, `sy-posture`, runs on each laptop and reports what it measures. If the report meets your policy, the
controller signs the laptop a **client certificate that lasts a few hours**. A port that requires a certificate from that CA then
admits only such laptops, for web applications and, through a local tunnel, for SSH, RDP and database ports.

The report is the device's own claim. It catches unmanaged, unpatched or unencrypted machines; it does not defend against someone
with full control of the machine. Keep certificates short (the default is 12 hours): a laptop that stops complying, or that you
revoke, keeps working until its certificate expires and not longer.

## Set up (once per tenant)

1. **A CA for the tenant's devices.** In OpenBao, enable a PKI mount (default name `pki-devices`, or set
   `SWITCHYARD_DEVICE_PKI_MOUNT`), create an issuer, and a role named after the tenant, for example `acme-devices`, with
   `client_flag=true`, `server_flag=false`, `allow_any_name=true` (the controller sets the names), `allowed_uri_sans=urn:switchyard:device:acme:*`
   and a maximum TTL of at least the certificate lifetime. One role per tenant, so one tenant's devices are never trusted by another's ports.
2. **Store the issuing CA** of that role as a client CA certificate record (see [TLS and client certificates](features/tls.md)); call it
   `acme-device-ca`.
3. **A posture policy:**

   ```yaml
   kind: posture
   name: managed-laptops
   pki_role: acme-devices            # must start with the tenant's name and a dash
   cert_ttl_h: 12
   requirements:
     allowed_os: [windows, macos, linux]
     minimums:
       - {os: windows, version: "10.0.19045"}
       - {os: macos, version: "14.0"}
     require_disk_encryption: true   # BitLocker, FileVault, LUKS on the system volume
     require_firewall: true
     require_screen_lock: false
     max_patch_age_days: 45          # days since the last system update was installed
   ```

4. **Require the certificate** on the service that should be reachable only from managed laptops:

   ```yaml
   ports:
     - {port: 443, tls: true, client_ca_id: <acme-device-ca>, client_verify: optional}   # or required
   # and, for part of a service, in an access set:
   rules:
     - match: {path_prefix: /internal}
       mode: login
       posture: {device_certificate: true}
   ```

   For a non-HTTP service use a `tcp` service on a TLS port with the same `client_ca_id` and `client_verify: required`.

## Enroll a laptop

An administrator creates a token (valid 24 hours, single use):

```bash
curl -X POST https://switchyard.example.test/api/v1/tenants/acme/device-enrollments \
  -H "Authorization: Bearer $SYCTL_TOKEN" -H "Content-Type: application/json" \
  -d '{"policy_id": "<id of managed-laptops>", "label": "Ana laptop"}'
```

On the laptop (BitLocker status needs an elevated prompt on Windows):

```bash
sy-posture enroll --controller https://switchyard.example.test --token sydev_... --name laptop-ana
sy-posture report          # prints "compliant; certificate valid until ..." or the failed requirements (exit code 3)
sy-posture run             # keeps reporting and renewing; start it from a scheduled task, launchd or systemd
sy-posture status
```

The agent keeps its state in your user configuration directory (or `--dir`, or `$SY_POSTURE_DIR`): `identity.json` and `tls.key`
are private to your account. Nothing secret leaves the machine; reports are signed, and only the public half of the certificate key is
sent.

Failure codes in a report: `os_not_allowed`, `os_too_old`, `disk_not_encrypted`, `firewall_off`, `screen_lock_off`,
`screen_lock_unknown`, `patch_too_old`, `patch_age_unknown`.

## Reach a TCP service (SSH, RDP, a database)

```bash
sy-posture tunnel --listen 127.0.0.1:2222 --to engine.example.test:8443 --server-name engine.example.test
ssh -p 2222 me@127.0.0.1
```

The tunnel wraps each connection in TLS with the current device certificate and refuses to listen on anything but a loopback address.
Use `--server-ca` when the engine's certificate comes from a private CA.

## Revoke and remove

`POST /tenants/{t}/devices/{id}/revoke` stops new certificates for a device (it can no longer report). The certificate it holds works
until it expires, at most `cert_ttl_h` hours later. `DELETE /tenants/{t}/devices/{id}` forgets the device so the name can be used again.
`GET /tenants/{t}/devices` shows each device, its last verdict, the facts it reported and its certificate serial and expiry.

## What to know

- A compliant report with a certificate request gets a new certificate each time (about every third of its lifetime); the old one
  stays valid until it expires.
- If the controller is down, devices keep working until their certificates expire.
- Keep the agent's clock right: reports more than five minutes off the controller's clock are refused.
- Without OpenBao, devices can enroll and report but receive no certificate.
- Windows and macOS measurements run the system's own tools (`manage-bde`, `netsh`, `fdesetup`, `softwareupdate` ...). They are
  tested against sample output, not on every OS version; check a first device of each kind with `sy-posture report`.
