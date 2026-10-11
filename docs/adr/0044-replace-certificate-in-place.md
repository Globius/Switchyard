# 0044. Replace a certificate in place

Status: Accepted
Date: 2026-10-11

## Context

Renewing an uploaded certificate meant importing a new object and editing every virtual service that used the old one.

## Decisions

1. `POST /tenants/{t}/certificates/{id}/replace` accepts the same material as import (`pem`, or `pkcs12_b64` and
   `password`; ADR 0043) and emits an `UPDATE` op on the existing certificate, so the id, the name and all usages stay.
2. The new bundle gets a new secret reference (it ends with the serial), so the apply pipeline sees a real difference
   and rolls it out through the canary like any change. The update is checked against the current object version.
3. After a replace the object is `source: upload`, `renew_policy: manual`: automatic renewal must not overwrite
   material the operator chose.
4. `syctl serve` runs uvicorn through the selector event loop on Windows, as the worker already did, because psycopg
   cannot use the Proactor loop that uvicorn picks there.
