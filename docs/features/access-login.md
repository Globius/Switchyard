# Login in front of a service

**What.** An `access` policy set puts an OpenID Connect login in front of a service, with per-path rules (public, any signed-in
user, group members, email domains), optional device posture, and identity headers for the origin. The engine runs
oauth2-proxy as a sidecar. A `directory` policy set lets a tenant sign in against LDAP, SAML, RADIUS or a local user list through a
broker (Dex) that the controller runs; RADIUS directories can chain several factors (a login flow) and rules can require a
number of them (`min_factors`).

**How to use.** Register a client at the identity provider, store the client secret and a cookie key in OpenBao under the
tenant's path, then create the set and attach it. The step-by-step guide with examples is
[Login in front of a service](../access.md).

**Limits.** Fails closed: if the sidecar is down, protected paths answer 503 and the origin is never reached without a login.
Plain ports need `https_redirect_port`. One access set per service. Ending one user's session early is not built: rotate the
cookie key (ends every session) or shorten the refresh interval. Device posture covers client certificates and ID-token
claims, not an endpoint agent. The origin can trust identity headers only if it accepts traffic from the engines alone.

**Decisions:** [0130](../adr/0130-auth-service-architecture.md), [0131](../adr/0131-access-rules-and-authorization.md),
[0132](../adr/0132-identity-headers.md), [0133](../adr/0133-auth-events.md),
[0137](../adr/0137-directory-logins-saml-ldap-local.md), [0138](../adr/0138-device-posture-signals.md),
[0195](../adr/0195-radius-login-and-step-up-flows.md).
