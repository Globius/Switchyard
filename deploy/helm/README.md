# Switchyard Helm chart

`switchyard/` runs the controller (API, worker, UI proxy, migrations) on Kubernetes. Read
[docs/deploy-kubernetes.md](../../docs/deploy-kubernetes.md) and [ADR 0191](../../docs/adr/0191-kubernetes-deployment.md).

- `switchyard/values.yaml`: every setting, commented.
- `test.sh`: lints and renders the chart with its value sets (`sh deploy/helm/test.sh`, needs helm; CI runs it).
- `bootstrap-test.sh`, `examples/openbao-test.yaml`: a one-node OpenBao and the Secrets, **for test installs only**.
