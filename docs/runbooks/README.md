# Runbooks

| Runbook | Use it when |
| --- | --- |
| [restore-database.md](restore-database.md) | The database is lost or a bad change must be undone in PostgreSQL (pgBackRest restore) |
| [rotate-ca.md](rotate-ca.md) | The PKI that signs agent and controller certificates is replaced or expiring |
| [replace-se.md](replace-se.md) | A service engine is broken, out of date or must move host |
| [emergency-rollback.md](emergency-rollback.md) | A bad configuration is live and the normal change flow is too slow |
| [telemetry.md](telemetry.md) | Charts or logs in the UI are empty, or the monitoring stack is being installed |
| [lab-hosts.md](lab-hosts.md) | Two Proxmox hosts must be prepared for the lab scenarios (KVM, IPVS, cross-host, ECMP) |
| [build-se-template.md](build-se-template.md) | New HAProxy, FRR, Keepalived or agent versions must reach new SEs |
| [rolling-upgrade.md](rolling-upgrade.md) | A whole SE group must move to a new template, or a group needs more engines |
| [login-broker.md](login-broker.md) | An access set uses an LDAP, SAML or local-user directory (the Dex broker) |
| [gslb-lab.md](gslb-lab.md) | GSLB is tried on the two-host lab, one site per host |

Each runbook says what to check first, the exact steps, how to confirm, and how to back out. The database
procedures are rehearsed on the lab cluster (`deploy/prod/lab-drill.sh`) before they are used for real.
Data planes keep serving while the controller or its database is down; nothing here is a reason to touch SEs
that are healthy.
