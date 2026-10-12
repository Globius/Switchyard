# 0142. WAF sidecar life cycle and failure mode

Status: Accepted
Date: 2026-10-12

## Context

08 asks for an ADR on the sidecar's life cycle and on fail-open versus fail-closed.

## Decisions

1. **Where it runs:** `coraza-spoa` runs as a systemd unit on every engine and listens on `127.0.0.1:9000` only. HAProxy reaches it through the
   backend `sy_coraza_spoa` (rendered once when any service uses a WAF). The unit starts only when `/etc/switchyard/coraza/config.yaml` exists.
2. **How it is configured:** the controller renders `coraza/config.yaml` into the signed agent bundle (the `coraza` directory is already on the
   agent's allowlist and maps to the `coraza-spoa` unit, which is reloaded when its files change). The per-service SPOE file is rendered with
   `haproxy.cfg`, so HAProxy and the agent files reach an engine in the same apply and the canary step covers both.
3. **Failure mode is per policy.** `fail_mode: open` (default) lets the request through when the agent does not answer within `timeout_ms`
   (default 100 ms); availability wins and the miss is logged. `closed` answers 503 when the agent did not answer, for services where an uninspected
   request is worse than an outage. The choice is explicit in the policy and shown in the UI; the data plane does not depend on the controller in
   either mode (an engine whose sidecar died keeps serving and applies the policy's choice on its own).
4. **Timeouts and load:** `timeout processing` follows `timeout_ms`; the agent has its own transaction TTL. The body is sent only with
   `inspect_body`, up to the engine buffer size, so a large upload is not buffered whole. Paths in `excluded_paths` skip the call entirely.
5. **Order in the frontend:** rate-limit and allow-list rules run first (cheap), the WAF verdict next, and a cached answer (ADR 0117) is served only
   after the WAF has passed the request.
6. Not part of this ADR: response-body inspection, bot defense and DDoS profiles (plan E10), and API schema validation (D5).
