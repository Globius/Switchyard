# 0141. Detect first, and a pinned Core Rule Set

Status: Accepted
Date: 2026-10-12

## Decisions

1. A new policy is in `detect` mode: Coraza runs in `DetectionOnly`, every request is inspected and logged, and HAProxy never acts on the verdict
   (no deny rule is rendered). A tenant reads the logs, disables rules that fire on legitimate traffic, then switches to `block`. There is no
   automatic learning (01).
2. `block` sets `SecRuleEngine On`; HAProxy refuses a request the agent marks `deny` with 403, and drops one marked `drop`.
3. The CRS release is a field with a short list of allowed values (`CRS_VERSIONS`, today `4.8.0`), because the SE image ships exactly the releases
   in that list. A policy therefore never changes behaviour when the image is updated; raising the version is a reviewed change set, and a new
   release is added to the list together with the image that carries it.
4. Paranoia level and the anomaly thresholds map to the CRS variables `tx.blocking_paranoia_level`, `tx.inbound_anomaly_score_threshold` and
   `tx.outbound_anomaly_score_threshold`; disabled rules to `SecRuleRemoveById`. The generated directives are in `render/waf.py` and covered by tests.
