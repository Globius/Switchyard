# 0228. JSON body schemas for API-schema protection

Status: Accepted
Date: 2026-10-24

## Context

[0197](0197-waf-api-schema-protection.md) made a WAF policy carry the operations an API accepts, and
enforced them in HAProxy: path, method, query names and types, content type, body size, traversal
sequences. Its "Not built" paragraph named JSON body validation as the missing piece, on the grounds
that HAProxy cannot parse a JSON body and Coraza "has no JSON Schema validator".

That second half is no longer true. Coraza has the `@validateSchema` operator: for a `.json`
argument it reads a JSON Schema document from the operator's root filesystem at the time the rules
are compiled, and returns true when the request body (taken from `TX:json_request_body`, which the
JSON body processor fills) does not match. The sidecar the SE image pins, `coraza-spoa` v0.9.0
(`se-image/versions.env`), links `github.com/corazawaf/coraza/v3` v3.7.0, which ships that operator
and its `kaptinlin/jsonschema` compiler.

So the question is no longer *whether* a body can be validated but *how a schema gets there* without
putting user text into a config, and what a failure does. The issue asks for that decision, the
stored schema, the generated rules in the per-group Coraza config the CRS rules already use, and the
documented limit that only documented shapes are checked.

## Decision

**A schema is a closed, typed tree on the operation; the controller generates the JSON Schema
document from it.** No user-supplied JSON Schema text, no template, no free-form directive.

### Storage (change-set time, untrusted input)

`ApiOperation.body` is an `ApiBody`: an object with 1 to 32 `properties` (`ApiBodyProperty`), each a
JSON member name the renderer can embed without escaping (`[A-Za-z_][A-Za-z0-9_]{0,63}`) and a
`kind` of `string`, `integer`, `number`, `boolean`, `uuid`, `enum` (with up to 32 `values`),
`object` (with nested `properties`) or `array` (with `items` of one scalar type). Every property may
be `required`, and carries the bounds that apply to its kind (`max_length` 1 to 4096 for strings,
`max_items` 1 to 1000 for arrays). `additional_properties` decides whether a member the schema does
not list is accepted.

Caps, enforced by the models so a malformed schema is an ordinary field error a change set reports
like any other:

| Bound | Value | Why |
| --- | --- | --- |
| nesting | 3 object levels | the recursive walk, the rendered document and the validator's own recursion |
| properties | 64 per body, 32 per object | a bounded document and a bounded render |
| enum values | 32 per property | same |
| operations with a body | 64 per schema | one generated file each; the agent bundle takes 200 files |
| string length | 1 to 4096 | keeps `maxLength` meaningful and the body limit the real bound |

Three cross-field rules, each a field error rather than a silent no-op:

1. a body needs `application/json` among the operation's `content_types`, because the JSON body
   processor only engages for that media type;
2. a body needs `inspect_body: true`, because otherwise the SPOE message carries no body at all
   (ADR 0144's event only adds `body=req.body` when the policy inspects it);
3. `api.mode: block` with a body needs the WAF itself in `block` mode: the sidecar runs
   `DetectionOnly` otherwise and would ignore the rule's `deny`.

### Renderer

`render/apischema.py` turns the tree into a JSON Schema document — only `type`, `properties`,
`required`, `additionalProperties`, `enum`, `maxLength`, `maxItems` and a fixed `pattern` for
`uuid` — and `render/waf.py` writes it into the same per-group Coraza file set as the CRS config:

```text
coraza/config.yaml                        the applications and their directives (unchanged)
coraza/schemas/<service>-<n>.json         one generated document per operation with a body
```

`<n>` is the operation's number in the order the HAProxy rules use (most specific path first, ties
in declared order), which is the number `txn.sy_api` carries, so a log line, a rule and a file name
the same operation. The document has no `$ref`, no `$schema` and no remote reference: it is a single
self-contained object, so the sidecar compiles it without network access.

Each document gets one chained `SecRule` in that service's `directives`, appended after the CRS
includes:

```text
# API body schema: POST /v1/pets (operation 2)
SecRule REQUEST_METHOD "@streq POST" "id:100002,phase:2,rev:1,log,auditlog,chain,msg:'api body schema violation',tag:'switchyard/api-body',logdata:'%{REQUEST_METHOD} %{REQUEST_FILENAME}',deny,status:400"
SecRule REQUEST_FILENAME "@rx ^/v1/pets$" "chain,t:none"
SecRule REQUEST_BODY "@validateSchema /etc/switchyard/coraza/schemas/pets-2.json" "t:none"
```

Every element is controller-generated: the method is a member of a fixed set, the path expression is
the one `apischema.path_regex` already builds and escapes for HAProxy, the file name comes from an
escaped service name and an integer, and the rule id is `100000 + n` (the CRS uses 900000 and up).
Coraza takes a chain's disruptive action from its first rule, so `deny` sits there; the checks
follow.

### Failure reporting

The verdict is Coraza's, so the report is Coraza's: a WAF log line (json, rule id, `msg`, `tag`,
`logdata` with the method and path) in both modes, and in `block` mode `400`. HAProxy's own capture
`txn.sy_apiv` does **not** see it — those rules run before the sidecar and record `path`, `method`,
`query`, `param`, `length`, `size` and `ctype` only. A malformed JSON body never reaches the schema:
`coraza.conf-recommended` rule 200002 answers 400 for a request body the processor cannot parse.

## Consequences

- No new component and no agent change beyond the extra bundle files under `coraza/`, which the
  agent already writes and the `coraza-spoa` unit already reads (`ReadOnlyPaths=/etc/switchyard/coraza`).
- The schema documents are compiled when the sidecar loads its rules, so a document that is missing
  or invalid stops `coraza-spoa` for the whole group. They are rendered by the same call that renders
  the `config.yaml` that names them, and a test asserts that every `@validateSchema` path in the
  directives is present in the file set.
- A large API costs files: 64 operations with a body per policy is a hard ceiling, not a tuning
  knob. A tenant with more splits the API across services (as with the HAProxy rule count, 0197).
- Only documented shapes are checked. `oneOf` / `anyOf` / `allOf`, arrays of objects, numeric ranges,
  string formats other than `uuid`, response bodies, headers and cookies are not represented; an
  operation that needs them declares the closest representable shape and keeps the CRS rules on for
  the content. This is the limit the docs now state.
- Verified three ways: unit tests run the generated documents through a draft 2020-12 validator
  (`jsonschema`, a dev dependency) for a valid body, a wrong type, a missing required member and an
  unknown member; the rule text is compared against a golden case (`34-api-body-schema`, written with
  `UPDATE_GOLDEN=1`) and a determinism test; and the rule shape was exercised against the same
  library revision the sidecar uses — coraza v3.7.0 in a scratch Go program — where a valid body
  passed and a wrong type, a missing required member, an unknown member, a bad enum and an over-long
  string were each answered 400. The container suite (`haproxy -c`, the coraza-spoa sample config)
  was not run for this change: no Docker on the branch host.
- Detect first still holds: `api.mode` defaults to `detect`, where the rule only logs, and a tenant
  that switches the schema to `block` must also switch the WAF, which the model says out loud.

## Alternatives considered

- **A fixed validator behind SPOE (a new sidecar).** It would keep JSON knowledge out of SecLang, but
  it is a new data-plane binary, a new unit, a new health signal and a new failure mode for a check
  Coraza already performs; and the engine image would have to ship it.
- **One hand-written SecRule per property, against `ARGS_POST:json.*`.** Coraza flattens a JSON body
  into `ARGS_POST` with dot-joined names, which is why property names here cannot contain a dot and
  why two members can collide (GHSA-5gj4-9gm7-2fx2); and the flattened value is a string, so a
  string `"3"` and a number `3` are indistinguishable. Type checking needs the document, not the
  pairs.
- **Storing the OpenAPI or JSON Schema document as user text.** Free-form config: it would put
  unvalidated text, remote references and unbounded recursion into the desired state and into a file
  the sidecar compiles at start. The typed tree is the same information with the bounds enforced.
- **Validating bodies in HAProxy.** HAProxy cannot parse a JSON body; the rules that could look at it
  are string matches (`payload(...)`) with no notion of type or required members.
