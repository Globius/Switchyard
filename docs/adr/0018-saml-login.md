# 0018. SAML 2.0 login

Status: Accepted

## Context

ADR 0030 (decision 10) specified SAML next to OIDC: SP-initiated web SSO, signed assertions, the
`InResponseTo` bound to the stored login state, an assertion replay cache, and SP metadata. This ADR
records how it was built (ticket G1-4).

## Decisions

1. **Library: `signxml` (with `lxml`), not pysaml2.** pysaml2 needs the `xmlsec1` binary, which the
   Windows development machines do not have and the controller image would have to add. `signxml` is
   pure Python on top of `lxml` and `cryptography`. The request/response handling is ~250 lines in
   `services/saml.py` and covered by unit tests.
2. **Trust only what the signature covers.** The verifier gets the certificates from the provider's
   settings (the certificate inside the response is never used). The assertion is then read from the
   element `signxml` reports as signed, so an unsigned assertion injected next to a signed one is
   ignored. Either the response or the assertion may be signed. DTDs and entities are refused before
   parsing; responses over 512 KiB are refused.
3. **Checks on the assertion:** issuer equals `idp_entity_id`; audience contains our `sp_entity_id`;
   `NotBefore`/`NotOnOrAfter` with 60 s skew; one bearer `SubjectConfirmation` whose `Recipient` is
   the ACS URL, whose `InResponseTo` is the request we sent, and which has not expired; a `Destination`
   that matches when the response is signed; status `Success`.
4. **State and replay.** `login_state` gains `request_id`; `RelayState` carries the state key. The row is
   deleted when read, so each AuthnRequest answers once, and a response with no state (IdP-initiated)
   is refused. Accepted assertion ids go into `saml_assertion_seen` until their expiry plus skew.
5. **One ACS URL.** The AuthnRequest, the metadata and the check all use
   `<public_url>/api/v1/auth/callback/<provider id>` (never the name), so they cannot differ.
6. **Storage.** `identity_provider` gets a `saml` JSON column (entity ids, SSO URL, public
   certificates, attribute names); `issuer_url` and `client_id` become nullable with a check that the
   settings match the protocol. There is no secret in a SAML row.
7. **Routes.** `POST /auth/callback/{provider}` takes `SAMLResponse` and `RelayState` form fields.
   Every failure audits `auth.login_failed` and redirects to `/login?error=<code>` with
   `provider_error`, `expired` or `no_access`. `GET /auth/saml/{provider}/metadata` is public and sends
   `Content-Disposition` with `?download=1`. No CSRF header applies, since no session exists yet.
8. **Configuration.** `syctl idp add-saml` until the identity-provider API (G1-3) exists. `sign_requests`
   and importing IdP metadata (`idp_metadata_url` / `idp_metadata_xml`) are not built; AuthnRequests
   are unsigned and the administrator enters entity id, SSO URL and certificates.

## Consequences

- Certificate rollover works by configuring two certificates for a while; each is tried in order.
- The `/login?error=` redirect is done for SAML only; the OIDC callback keeps returning JSON until G1-2.
- Signed AuthnRequests and metadata import are follow-ups, not blockers for Authentik or Entra ID.
