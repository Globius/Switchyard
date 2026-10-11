# 0064. Certificate renewal window

Status: Accepted
Date: 2026-10-11

## Context

Auto-renewal ran 30 days before expiry, a constant in the renewal job. The certificate screen shows "Auto, 30 days before"
and has no way to say anything else.

## Decisions

1. `certificate.renew_before_days` (integer, 1 to 365, default 30; migration 0015) is part of the certificate object. It is
   returned on every read and set with a normal create or update operation in a change set.
2. The daily renewal job renews an `auto` ACME or Cloudflare Origin certificate when `not_after - renew_before_days <= now`.
   Certificates made before the field existed keep the old 30 days.
3. The field is ignored for `manual` certificates, which are never renewed by the controller.
