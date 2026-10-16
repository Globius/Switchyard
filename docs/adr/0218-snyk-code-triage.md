<!--
SPDX-FileCopyrightText: 2026 The Switchyard contributors

SPDX-License-Identifier: Apache-2.0
-->
# 0218. Snyk Code triage: fix real findings, ignore the rest with a reason

Status: Accepted
Date: 2026-10-16

## Context

The first Snyk Code scan reported hardcoded secrets and passwords, path traversal, insecure XML parsers, SSRF and
Jinja autoescape off.

## Decision

- **XML:** the Route 53 and S3 adapters parse with `defusedxml`. SAML metadata already uses a hardened lxml parser.
- **Jinja autoescape:** stays off in `render/haproxy.py` and `render/routing.py`. The output is HAProxy/FRR text; HTML
  escaping would corrupt quotes and `&`. Safety comes from the validators and `render.escape`. Inline ignore, no code change.
- **Paths:** values from the API or renderer are already allowlisted (`bundle_staging._NAME`, `backup_targets._checked`).
  `write_broker_files` gained a containment check.
- **SSRF:** DNS provider and Kubernetes endpoints already pass `check_endpoint` (CIDR policy).
- **Secrets and passwords:** all remaining hits are dummy values in tests and fixtures. `.snyk` excludes those paths.

## Consequences

New runtime XML parsing must use `defusedxml`. New ignores need a reason in `.snyk` or inline.
