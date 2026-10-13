# Runbook: the login broker (Dex)

Needed only when an access set uses a `directory` (LDAP, SAML, RADIUS or local users; ADR 0137, 0195). Engines do not run it.

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
6. RADIUS directories are reached through the login gate (`broker-gate`, `syctl broker gate --dir /broker/gate`, loopback port 5599). The
   generated `routes.conf` already sends their paths to it; run two for availability (state is in signed cookies, nothing is shared).

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
| RADIUS sign-in says "The sign-in service is unavailable" (503) | the gate cannot get an authentic answer: wrong shared secret, server not reachable over UDP from the broker host, or the NAS (broker host) is not a client of the RADIUS server |
| RADIUS users are all refused with "Sign-in failed." | the user/password is wrong, or the server rejects the NAS-Identifier; check the RADIUS server's log |
| A step-up redirect goes to a 400 page | the host in the request is not one of the directory's access sets' `redirect_hosts`, or the path contains characters a path cannot hold |
| "min_factors" render fails with "no login broker URL" | `SWITCHYARD_AUTH_BROKER_URL` is not set on the worker that renders |
| Users are signed out every 15 minutes | the broker was unreachable at the cookie refresh; fix reachability from the engines |
