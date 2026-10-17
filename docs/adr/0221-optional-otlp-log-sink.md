# 0221. Optional OTLP log sink on engines, Loki optional

Status: Accepted
Date: 2026-10-17

## Context

Engines ship HAProxy, Coraza and sy-auth logs with Vector to one Loki sink, configured through
`/etc/switchyard/vector.env`. Some operators already run an OpenTelemetry collector and want the same events there, and
some run no Loki at all.

## Decision

- Vector cannot enable a sink from an environment variable, so `se-image/files/vector.yaml` keeps the sources and
  transforms and a small script, `sy-vector-sinks` (run as the unit's first `ExecStartPre`), writes the sinks into
  `/run/switchyard-vector/sinks.yaml`. A Loki sink exists only when `LOKI_URL` is set, an OTLP sink only when
  `OTLP_LOGS_URL` is set. Both unset is valid: no log sink. The script refuses values with `"`, `\`, `$` or a newline, so
  an environment value cannot alter the YAML.
- The OTLP sink is a plain `http` sink posting OTLP/HTTP JSON to `<OTLP_LOGS_URL>/v1/logs`. A transform builds one
  `ExportLogsServiceRequest` per event (the sink batches one event, as a JSON body holds one request). Resource attributes
  mirror the Loki labels (`tenant`, `tenant_name`, `se_group`, `engine`, `frontend`) plus `service.name` per source.
- Optional keys: `OTLP_LOGS_HEADERS` (written to a 0600 file in a 0700 runtime directory, never echoed),
  `OTLP_LOGS_INSECURE_SKIP_VERIFY` (default false) and `OTLP_LOGS_CA_FILE`.
- Each sink has its own bounded memory buffer (10 000 events). **Deviation from the request:** Vector's buffers offer
  only `block` and `drop_newest`, not drop-oldest. `drop_newest` never blocks, so one unreachable sink cannot stall or
  starve the other; the cost is that the oldest queued events are kept and the newest discarded. Drops are counted in
  `buffer_discarded_events_total`, exposed by a loopback-only `prometheus_exporter` on `127.0.0.1:9598` (Vector's own
  counters, not log or metric shipping).

## Consequences

Default behaviour with only `LOKI_URL` is unchanged apart from the loopback exporter. An OTLP-only engine does not feed
the controller's Loki-backed views. One request per record is wasteful at high volume; a batching OTLP encoder can
replace it later. Tests: `controller/tests/integration/test_vector_otlp.py` (validate for neither, Loki, OTLP and both;
one HAProxy line to both sinks) and the SE image check.
