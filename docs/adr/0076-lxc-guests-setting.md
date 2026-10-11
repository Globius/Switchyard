# 0076. A system setting for container (LXC) guests

Status: Accepted
Date: 2026-10-11

## Context

An LXC engine runs as a privileged container with nesting, so root in the guest is root on the Proxmox host. It is a lab option (no KVM), but
`guest_kind: lxc` was open to every tenant administrator in production (docs/security/backend-review.md, S4).

## Decisions

1. A system setting `allow_lxc_guests`, default **off**, stored in `system_setting` (migration 0022). Only system administrators change it:
   `GET` and `PATCH /system/settings` (`{allow_lxc_guests}`). Changes are audited as `system.settings_changed`.
2. While it is off, `POST /tenants/{t}/se-groups/{group}/deploy` with `guest_kind: lxc` answers `403` with code `lxc_disabled` and a message that says
   what to do. The deploy service refuses it too, so no other caller can queue one. VM deploys are unaffected.
3. `SWITCHYARD_ALLOW_LXC=1` forces the setting on for a deployment (the lab and the quickstart), as `SWITCHYARD_BREAK_GLASS=1` does for break-glass.
   `GET /system/settings` then shows `allow_lxc_guests_forced: true`; the stored value has no effect while it is set. `syctl setting allow_lxc_guests true`
   writes the stored value directly in the database for scripted set-ups.
4. `GET /me` has `features: {lxc_guests}` so the UI can show or hide the Container option for everyone without calling an admin route.
5. A job queued while the setting was on still runs after it is switched off; the setting gates new deploys only.
