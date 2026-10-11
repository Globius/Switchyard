# 0060. Audit export endpoint

Status: Accepted
Date: 2026-10-11

## Context

The audit log screen exported only the events already loaded in the browser (06 planned-vs-built).

## Decisions

1. `GET /tenants/{t}/audit-events/export` and, for system administrators, `GET /audit-events/export` stream every event
   that matches the list filters (`actor`, `action`, `object`, `since`, `until`, `q`; `tenant` on the system route),
   newest first, as `csv` (default) or `jsonl` (`format`). `cursor` and `limit` are ignored.
2. The export reads the log in pages of 500, each in its own short transaction, so a large export does not hold a
   connection or a snapshot open. There is no row cap; the caller narrows with `since` and `until`.
3. Who may call it equals who may list: any tenant role on the tenant route, `system-admin` on the system route.
4. Each export writes an audit event `audit.export` with the format and filters, so a download of the log is itself logged.
5. CSV columns: `at`, `tenant_id`, `actor`, `action`, `object`, `request_id`, `diff` (JSON text). JSON Lines has one
   event object per line with the same fields as the list.
