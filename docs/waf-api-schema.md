# API schema protection (WAF)

A WAF policy can also hold a **schema of your API**. Requests that are not in it are refused (or only logged while you check
it): wrong method, unknown path, a query parameter that is not listed or has the wrong type, a body of the wrong type or
size. This is a positive model, so it catches tampering that attack patterns miss. It runs in HAProxy, before the Coraza
rules, and needs no extra component (ADR 0197).

## Make a schema from your OpenAPI file

```bash
syctl waf api-schema openapi.yaml > api.json           # detect mode; warnings go to stderr
syctl waf api-schema openapi.yaml --mode block --max-body-bytes 1048576
```

The command reads OpenAPI 3.x or Swagger 2.0. It takes the base path from `servers` (or `basePath`), the methods and paths, the
types of path and query parameters, which query parameters are required, and the media types of request bodies. Operations or
parameters it cannot represent are listed as warnings and left out. Paste the output into a `waf` policy set:

```yaml
kind: waf
name: pets-waf
mode: detect                 # the CRS part, independent of the schema's own mode
api:
  mode: detect               # start here
  base_path: /v1
  max_body_bytes: 1048576
  operations:
    - method: GET
      path: /pets
      query:
        - {name: limit, kind: integer}
        - {name: status, kind: enum, values: [available, sold], required: true}
    - method: GET
      path: /pets/{id}
      path_params: [{name: id, kind: integer}]
    - method: POST
      path: /pets
      content_types: [application/json]
      max_body_bytes: 65536
```

## Switch to blocking

In `detect` mode every request that breaks the schema is let through, and the reason is added to the access log line in the
`{...}` block before the request line: `path`, `method`, `query`, `param`, `length`, `size` or `ctype`. When the lines show only
requests you want refused, set `api.mode: block`. Then:

| Reason | Status | Meaning |
| --- | --- | --- |
| `path` | 404 | no operation has this path (or it has dot segments, `//` or an encoded `.` `/` `\`) |
| `method` | 405 | the path exists, not with this method |
| `query`, `param` | 400 | unknown query parameter, wrong type or value, or a required one is missing |
| `length` | 411 | a chunked body on an operation that has a size limit |
| `size` | 413 | `Content-Length` over the limit |
| `ctype` | 415 | body media type not in `content_types` |

A path parameter of the wrong type (`/pets/abc` for an integer `id`) matches no operation, so it is a `path` 404, not a `param` 400.

## Details

- Path parameters: `integer`, `number`, `boolean`, `uuid`, `enum`, or the default (any segment without `/`, up to `max_length`).
- The whole query string is checked pair by pair, so `?limit=1&limit=x` fails on the second `limit`. With
  `allow_unknown_query: true` unknown parameters pass, and the listed ones are checked on their first occurrence only.
- When two operations could match the same path, the one with more literal segments wins (`/orders/mine` before `/orders/{ref}`).
- `OPTIONS` (CORS pre-flight) is refused unless you list it as an operation.
- Not covered: JSON bodies against their schema, responses, headers and cookies. Keep the CRS rules on for the body content.
