"""Render the files of one SE group from the database."""

from sqlalchemy.ext.asyncio import AsyncEngine

from switchyard.db.repository import find_se_group_id, find_tenant_id, load_snapshot
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.render.haproxy import RenderResult, render_haproxy
from switchyard.services.errors import NotFoundError


async def render_se_group(
    admin: AsyncEngine, app: AsyncEngine, tenant_name: str, se_group_name: str
) -> RenderResult:
    """Load one SE group's state and render its HAProxy files.

    The tenant is resolved with the admin role (a name lookup has to see every tenant). The state
    itself is then read with the RLS-subject application role, scoped to that tenant, so the render
    can only ever see data that tenant owns.

    Args:
        admin: Engine connected as the admin role.
        app: Engine connected as the application role.
        tenant_name: Tenant that owns the SE group.
        se_group_name: SE group to render.

    Returns:
        The rendered files and warnings.

    Raises:
        NotFoundError: If the tenant or SE group does not exist.
        switchyard.domain.errors.DomainValidationError: If the stored state breaks a domain rule.
        switchyard.render.escape.RenderError: If a value cannot be rendered safely.
    """
    async with admin_session(admin) as session:
        tenant_id = await find_tenant_id(session, tenant_name)
    if tenant_id is None:
        raise NotFoundError(f"tenant {tenant_name!r} not found")
    async with tenant_session(app, [tenant_id]) as session:
        group_id = await find_se_group_id(session, tenant_id, se_group_name)
        if group_id is None:
            raise NotFoundError(f"SE group {se_group_name!r} not found in tenant {tenant_name!r}")
        snapshot = await load_snapshot(session)
    return render_haproxy(snapshot, group_id)
