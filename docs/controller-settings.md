# The controller's own address, VIP and certificate

In production the controller is reached through the `controller` virtual service of the locked
`management` tenant, served by the management SE group (ADR 0051, `deploy/prod/README.md`). Changing its
host name, VIP or certificate can cut you off from the screen you used, so the change is a transaction
with a way back (ADR 0081). System administrators do it under **Admin > Controller**, with
`PUT /system/controller/pending`, or by hand as below.

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

## Without a management tenant

In a quick start or development setup there is no `management` tenant. `GET /system/controller` then says
`managed: false`; only the FQDN can be set, it becomes the public URL at confirm, and the address, VIP
and certificate stay with whatever fronts the controller (a reverse proxy, `SWITCHYARD_PUBLIC_URL`).

## Environment

`SWITCHYARD_PUBLIC_URL` is the fallback until a confirmed URL exists. A confirmed URL overrides it.
