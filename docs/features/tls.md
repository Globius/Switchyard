# TLS on the front end

**What.** TLS termination with SNI, protocol and cipher profiles, OCSP stapling, client-certificate verification (mTLS) and
SNI passthrough for TLS that must stay end to end.

**How to use.**

```yaml
ports:
  - port: 443
    tls: true
    http2: true
    tls_profile: intermediate      # modern (TLS 1.3), intermediate (1.2 and 1.3), legacy (1.0 to 1.3)
    client_ca_id: <client CA id>   # optional: verify client certificates
    client_verify: optional        # required (default) refuses clients without a valid certificate
certificate_ids: [<default>, <other names>]
```

- Client CAs are imported with `POST .../certificates/import-ca` (`{name, pem, crl_pem?}`); the CRL is checked by HAProxy.
- `ocsp_stapling: true` on a certificate makes the engine fetch and staple OCSP answers itself.
- **SNI passthrough:** on a `tcp` service set `tls_passthrough: true` on the ports and add host-only routes; the engine reads
  the SNI from the client hello and picks the pool without decrypting. `default_pool_id` takes clients with no or unknown SNI.
- Who a client certificate belongs to, and which paths need one, is a [`client_cert` policy set](api-protection.md).

**Limits.** Cipher lists are fixed constants from Mozilla's server-side TLS guidance; there is no custom cipher string. A
passthrough service has no HTTP features (headers, redirects, compression, WAF). OCSP stapling is refused on Cloudflare
Origin certificates, which have no responder.

**Decisions:** [0067](../adr/0067-tls-profiles-and-ocsp.md), [0070](../adr/0070-client-mtls.md),
[0115](../adr/0115-sni-passthrough.md).
