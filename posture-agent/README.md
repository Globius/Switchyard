# sy-posture: the Switchyard device posture agent

Runs on a laptop, measures it (operating system version, disk encryption, firewall, screen lock, days since the last update), reports to
the controller, and keeps the short-lived client certificate the controller signs when the laptop meets its policy. A `tunnel` command
carries local TCP connections (SSH, RDP, database clients) to services behind a Switchyard engine over TLS with that certificate.

Design and limits: [ADR 0196](../docs/adr/0196-device-posture-agent-and-device-certificates.md). User guide:
[Device posture and device certificates](../docs/device-posture.md).

```bash
go build -o sy-posture ./cmd/sy-posture      # Go 1.26, standard library only
go vet ./... && go test ./...
```

Layout: `internal/protocol` (the facts, the signed report, the client; its `testdata/vector.json` is checked against the controller),
`internal/facts` (measuring and the pure parsers of the tools' output), `internal/state` (identity and keys on disk), `internal/tunnel`.
The end-to-end test against a real controller and HAProxy is `controller/tests/integration/test_posture_agent_e2e.py`.
