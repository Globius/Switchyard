"""OpenBao policy for the controller, matching the secret namespaces the code allows.

The controller checks secret references in code (``domain.validators.check_secret_ref_scope``);
this policy is the second layer: even a bug in the code cannot let the controller's token read
outside the tenant namespaces and the shared prefixes it was given. ``syctl openbao-policy``
prints it.
"""

from collections.abc import Sequence


def controller_policy(
    shared_prefixes: Sequence[str] = (), *, mount: str = "kv", platform: Sequence[str] = ()
) -> str:
    """Return the HCL policy for the controller's OpenBao token.

    Args:
        shared_prefixes: Shared paths tenants may name (``kv/se/``), read-only for the controller.
        mount: The KV v2 mount (references are ``<mount>/<path>``).
        platform: Platform secrets the controller itself reads, for example ``kv/proxmox/token``.

    Example:
        >>> policy = controller_policy(["kv/se/"], platform=["kv/proxmox/token"])
        >>> 'path "kv/data/tenants/*"' in policy and 'path "kv/data/se/*"' in policy
        True
        >>> 'path "kv/data/proxmox/token"' in policy and 'deny' in policy
        True
    """
    lines = [
        "# Switchyard controller (`syctl openbao-policy`). Review before loading.",
        "# Tenant namespaces: the controller stores and reads their certificates.",
        f'path "{mount}/data/tenants/*" {{',
        '  capabilities = ["create", "read", "update"]',
        "}",
        f'path "{mount}/metadata/tenants/*" {{',
        '  capabilities = ["list", "read"]',
        "}",
    ]
    for prefix in shared_prefixes:
        path = prefix.removeprefix(f"{mount}/").rstrip("/")
        lines += [
            f"# Shared with every tenant ({prefix}); read-only.",
            f'path "{mount}/data/{path}/*" {{',
            '  capabilities = ["read"]',
            "}",
        ]
    for ref in platform:
        path = ref.removeprefix(f"{mount}/")
        lines += [
            f"# Platform secret ({ref}); read-only, never named by tenant objects.",
            f'path "{mount}/data/{path}" {{',
            '  capabilities = ["read"]',
            "}",
        ]
    for path in ("break-glass", "switchyard/break-glass"):
        lines += [
            f"# Break-glass administrator TOTP secret ({mount}/{path}/*): API and local-admin.",
            f'path "{mount}/data/{path}/*" {{',
            '  capabilities = ["create", "read", "update", "delete"]',
            "}",
        ]
    lines += [
        "# Everything else is denied by default: no other path, no delete, no destroy.",
        f'path "{mount}/delete/*" {{',
        '  capabilities = ["deny"]',
        "}",
        f'path "{mount}/destroy/*" {{',
        '  capabilities = ["deny"]',
        "}",
    ]
    return "\n".join(lines) + "\n"
