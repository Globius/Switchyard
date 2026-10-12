# terraform-provider-switchyard

OpenTofu (and Terraform) provider for the Switchyard controller. The resource schemas are **generated from
`docs/api/openapi.json`**; the behaviour of every resource is one piece of generic code. ADR 0146.

| Type | Resource | Data source |
| --- | --- | --- |
| `switchyard_network`, `_vip_pool`, `_se_group`, `_virtual_service`, `_pool`, `_health_monitor` | yes | yes |
| `switchyard_redirect_policy_set`, `_allow_list_policy_set` | yes | yes |
| `switchyard_vrrp_routing_profile`, `_bgp_routing_profile` | yes | yes |
| `switchyard_certificate` | no | yes (key material lives in OpenBao; issue and import with the API or `syctl`) |

Data sources look an object up by `name`. Resources import by id: `tofu import switchyard_pool.web <uuid>`.

## Use it

```hcl
provider "switchyard" {
  endpoint = "https://switchyard.example.test"   # or SWITCHYARD_ENDPOINT
  tenant   = "acme"                              # or SWITCHYARD_TENANT
  # token comes from SWITCHYARD_TOKEN; ca_file / SWITCHYARD_CA_FILE trusts a private CA
}
```

The token is an API token (`sy_...`) with the operator role or better in the tenant. See `examples/main.tf`.

Until the provider is published, build it and point OpenTofu at the binary:

```sh
cd provider && go build -o ~/go/bin/terraform-provider-switchyard .
```

```hcl
# ~/.tofurc
provider_installation {
  dev_overrides { "switchyard/switchyard" = "/home/you/go/bin" }
  direct {}
}
```

## How a change behaves

- Every create, update and delete is **one change set applied at once** (`?apply=true`): validation, canary, revision, Git
  mirror, exactly like an API call. The provider waits for the apply job, and reports a rejected or reverted change (a failed
  canary, for example) as an error with the controller reason. `apply_timeout` (default 10 minutes) bounds the wait.
- Writes are sent **one at a time**; `-parallelism` does not speed them up, because the controller applies per SE group behind a
  lock.
- Updates send the object version as `If-Match`. Someone changing the object in between gives an error; run `tofu refresh`.
- A tenant that **requires approval** refuses `apply=true` from this provider (only a break-glass administrator may bypass). Use
  change sets through the API or the UI for those tenants.
- Object ids are chosen by the provider (UUIDv7) so dependent resources can refer to them.
- Attributes you leave out take the controller default and show as known after apply.
- A pool with `discovery` owns its `members` (ADR 0145): leave `members` out of the configuration.

## Developing

`internal/provider/zz_generated.go` is generated. After the API changes:

```sh
UPDATE_OPENAPI=1 uv run pytest tests/unit/test_openapi.py   # in controller/
cd provider && go generate ./...
```

`go test ./...` fails when the committed table is out of date (`TestTheGeneratedTableIsCurrent`). Tests run against an in-memory
fake of the controller API and need no OpenTofu binary.
