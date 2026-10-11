# 0074. Bundles bound to an engine and a sequence; named callers for the agent

Status: Accepted
Date: 2026-10-11

## Context

Two low-severity findings of the security review (docs/security/backend-review.md, A3 and A4): a correctly signed bundle could be sent
again later (rolling an engine back) or to another engine, and any certificate from the agents' CA could call the agent's status and
probe calls.

## Decisions

1. The signed `Bundle` has two new fields: `engine` (the engine's name) and `sequence` (milliseconds since the Unix epoch, strictly
   increasing). The controller's signer fills them: the engine name from the agent record, the sequence from a clock that never repeats a value.
2. The agent takes its own name from the common name of its certificate (the controller puts the engine name there at enrolment). It refuses a
   bundle for another engine, and a bundle whose sequence is not above the last one it applied. The last sequence is kept in
   `/etc/switchyard/agent/bundle-seq`, so a restart does not reopen the replay window. A refused bundle changes nothing.
3. Bundles without an engine or sequence (from controllers older than this change) are refused unless the agent runs with
   `-allow-unbound-bundles`. Upgrade agents and controllers together, or start agents with the flag until the controller is upgraded, then drop it.
4. Controllers that share a signing key need clocks that agree within the time between two applies. A rollback is a new bundle with a new
   sequence, so it works like any apply.
5. `sy-agent -controller-cn controller` (comma-separated) limits who may call the agent to client certificates with that common name or DNS name,
   after the CA check. Without the flag any certificate signed by the CA may call (a warning is logged), which keeps existing installs working;
   set it to the name of the controller's client certificate.
