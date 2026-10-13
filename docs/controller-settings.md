# The controller's own address, VIP and certificate

In production the controller is reached through the `controller` virtual service of the locked
`management` tenant, served by the management SE group (ADR 0051, `deploy/prod/README.md`). Changing its
host name, VIP or certificate can cut you off from the screen you used, so the change is a transaction
with a way back (ADR 0081). System administrators do it under **Admin > Controller**, with
`PUT /system/controller/pending`, or by hand as below. To go **back to the address from the
environment** (`SWITCHYARD_PUBLIC_URL`), propose `{"reset_to_default": true}` instead of an `fqdn` (see
"Going back to the default address" below, or `syctl api controller-reset`).

## What exists out of the box

`syctl serve` creates the empty, locked `management` tenant if it is missing (ADR 0155). The page then
shows `management_tenant: true`. Listen address and certificate become changeable when the tenant also
holds a `controller` service (deployed or adopted, `docs/operating.md`); until then only the public
address can change. In that state it may be an **IP address** (IPv4 or IPv6, for a lab or quick start);
`reached_at` in the response is the address you are using now. With a `controller` service it must be a
host name.

## The flow

1. **Prepare.** Put the certificate in the management tenant (import it or request it from an ACME
   provider, `docs/acme.md`). It must be a server certificate, valid for more than a day, with a name
   that covers the new FQDN. Point DNS at the VIP. **Allow the new redirect addresses in your identity
   providers first**: `GET /system/controller` lists them under `redirect_urls` for a pending change
   (OIDC `https://<fqdn>/api/v1/auth/callback/<provider>`, SAML `.../callback/<provider id>`).
2. **Propose.** `PUT /system/controller/pending` with `fqdn` and, if they change, `vip` and
   `certificate` (and `confirm_within_s`, 30 to 600, default 120). The controller checks the FQDN, the
   certificate (covers the FQDN, not about to expire, the service has a TLS port) and the VIP (usable
   on the group, in its pool), then applies the change to the `controller` service as a change set of
   the management tenant. It goes through the usual canary and rollout, with the recorded reason
   `controller settings: <who> changes <fqdn>` (the tenant's lock needs one).
3. **Confirm.** When the change set has applied, the window starts (`pending.state` is `waiting`,
   `seconds_left` counts down). Open `https://<new fqdn>`, sign in and press *Keep the new settings*
   (`POST /system/controller/confirm`). The request must arrive on the new FQDN, because reaching it is
   the proof it works; it makes the FQDN the public URL (login redirects, cookies, SAML entity ids).
4. **Revert.** *Revert now* (`POST /system/controller/revert`) works from either address. If nobody
   confirms before the window ends, the **worker** reverts it: another change set puts the previous VIP
   and certificate back. The worker talks to the engines directly, so this works even when the API cannot
   be reached through the new address. Only those two fields are restored, so other edits made to the
   management tenant meanwhile survive.

Only one change can wait at a time (`409`). A change set that cannot apply (a canary refuses it, the
validator objects) ends the transaction at once and `last_result` says why.

## Without the UI

On a controller node, with database access only:

```text
syctl controller status      # public URL, pending change, last result
syctl controller revert      # revert a waiting change; the worker applies it
```

The break-glass login (`docs/auth.md`) and the lab runbooks still apply. A change set of the
management tenant can also be rolled back like any other (`POST /tenants/management/revisions/{n}/rollback`).

## Cluster members, including the one on ESXi

The controller runs on three VMs, each with the API, a worker, etcd and a Patroni-managed PostgreSQL (`docs/production.md`).
In the interim placement two are on Proxmox hosts and the third is on vSphere/ESXi until a third Proxmox host arrives. That
VM is outside the `management` tenant and tagged `nofailover`: it votes and replicates but never becomes the primary. The
platform does not manage it, so it is listed as an **external member**.

**Admin > Controller > Cluster members** lists the VMs with where each runs, its address, the role Patroni reports (Primary,
Sync standby, Replica, or "Not in the cluster"), its state, replication lag and whether it can take over. External members carry
an *External* badge. The roles come from Patroni's `GET /cluster` on the first listed member that answers, matched on the
member name, so `name` must be the Patroni member name (`PATRONI_NAME`). Without any `patroni_url` the list shows the VMs and no
roles.

```yaml
# in the adoption file (deploy/prod/adopt/management.example.yaml), or POST /system/controller/members
controller_members:
  - {name: ctl-1, address: 10.0.0.11, platform: proxmox, host: pve1, patroni_url: "http://10.0.0.11:8008"}
  - {name: ctl-esxi, address: 10.0.0.13, platform: esxi, host: esxi-01, nofailover: true, patroni_url: "http://10.0.0.13:8008"}
```

- The list only describes the VMs. Adding or removing an entry never touches a VM, etcd or Patroni; to retire a member, run
  `etcdctl member remove` and `patronictl remove` first (02, "Interim placement"), then remove the entry.
- System administrators only. Patroni's REST port has no authentication, so keep it on the controllers' private network; the
  controller refuses loopback, link-local and multicast targets.
- API: `GET`/`POST /system/controller/members`, `PATCH`/`DELETE /system/controller/members/{id}` (`If-Match`).
  Audit: `controller_member.created`, `.updated`, `.deleted`. Decision: [0051](adr/0051-management-tenant-lock-and-adopt.md).

## Without a management tenant

In a quick start or development setup there is no `management` tenant. `GET /system/controller` then says
`managed: false`; only the FQDN can be set, it becomes the public URL at confirm, and the address, VIP
and certificate stay with whatever fronts the controller (a reverse proxy, `SWITCHYARD_PUBLIC_URL`).

## Environment

`SWITCHYARD_PUBLIC_URL` is the fallback until a confirmed URL exists. A confirmed URL overrides it.

## Going back to the default address

To drop the configured public URL (the address from `SWITCHYARD_PUBLIC_URL` or the request applies again), propose `{"reset_to_default": true}` to `PUT /system/controller/pending` (or `syctl api controller-reset`) and confirm as usual. It changes only the public URL, so `vip` and `certificate` are refused with it, and no change set is opened.
