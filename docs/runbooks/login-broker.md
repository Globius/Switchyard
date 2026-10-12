# Runbook: the login broker (Dex)

Needed only when an access set uses a `directory` (LDAP, SAML or local users; ADR 0137). Engines do not run it.

## Set up

1. Choose the public base URL, for example `https://auth.example.test/dex`. Users' browsers **and every engine** must reach it.
2. On the controller nodes set `AUTH_BROKER_URL` (compose) and start the extra services:
   `docker compose --env-file node.env -f compose.yaml -f compose.broker.yaml up -d`.
3. The `controller` services need the same URL as `SWITCHYARD_AUTH_BROKER_URL`, so that engine bundles name the broker as issuer.
4. Include the generated nginx routes in the public proxy and reload it after a directory is added or removed:
   the file is `dex/routes.conf` in the `broker-config` volume (`docker compose exec proxy nginx -s reload` after copying it to
   `/etc/nginx/broker/routes.conf`, and `include /etc/nginx/broker/routes.conf;` in the server block that serves the broker URL).
   TLS terminates at the proxy.
5. `broker-render` re-renders every 30 seconds; `broker` starts, restarts and stops one Dex per directory.

## Check

- `docker compose logs broker` shows `broker: started <tenant>-<directory>` for each directory in use.
- `curl https://auth.example.test/dex/<tenant>/<directory>/.well-known/openid-configuration` returns the issuer.
- A sign-in through a protected service reaches the broker's form (local users) or the directory's page (SAML).

## Problems

| Symptom | Cause |
| --- | --- |
| Engine bundle fails with "no login broker URL" | `SWITCHYARD_AUTH_BROKER_URL` is not set on the worker |
| `syctl broker render` says a secret is not found | the bind password, password hash or client secret path is missing in OpenBao |
| Login page "Invalid client_id" | the access set's `client_id` or `redirect_hosts` changed and the broker has not re-rendered yet (30 s) |
| LDAP sign-in fails for everybody | the bind account or `user_filter` is wrong; Dex logs the search it ran |
| Users are signed out every 15 minutes | the broker was unreachable at the cookie refresh; fix reachability from the engines |
