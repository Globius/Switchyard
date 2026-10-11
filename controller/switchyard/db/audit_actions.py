"""The catalog of audit event actions.

Every ``audit_event.action`` the controller writes is listed here with a category and a sentence the
UI can show as its label. A test scans the source for ``action=`` literals and fails when one is
missing, so the catalog and ``GET /audit-actions`` cannot drift from the code.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AuditAction:
    """One kind of audit event.

    Attributes:
        action: The dotted name stored in ``audit_event.action``.
        category: The part before the dot; a UI can group and filter by it.
        description: What happened, in a sentence.
    """

    action: str
    category: str
    description: str


def _a(action: str, description: str) -> AuditAction:
    return AuditAction(action, action.split(".", 1)[0], description)


CATALOG: tuple[AuditAction, ...] = (
    _a("audit.export", "Audit events were downloaded as a file."),
    _a("auth.break_glass_failed", "A break-glass login failed."),
    _a("auth.break_glass_login", "The break-glass administrator signed in."),
    _a("auth.local_admin_created", "A local administrator was created from the command line."),
    _a("auth.login", "A user signed in."),
    _a("auth.login_denied", "A sign-in was refused because the user has no access."),
    _a("auth.login_failed", "A sign-in failed (provider error, bad assertion or expired state)."),
    _a("auth.logout", "A user signed out."),
    _a("certificate.client_ca_imported", "A client CA bundle was imported."),
    _a("certificate.imported", "A certificate and its key were imported."),
    _a("certificate.renewal_failed", "Renewing a certificate failed."),
    _a("change_set.add_ops", "Operations were added to a draft change set."),
    _a("change_set.apply", "A change set was applied."),
    _a("change_set.applied", "A change set finished applying and became a revision."),
    _a("change_set.approval_voided", "An edit voided a pending approval request."),
    _a("change_set.approve", "A change set was approved and applied."),
    _a(
        "change_set.break_glass_apply",
        "A system administrator applied a change set bypassing approval.",
    ),
    _a("change_set.discard", "A draft change set was discarded."),
    _a("change_set.failed", "Applying a change set failed or was reverted."),
    _a("change_set.open", "A draft change set was opened."),
    _a("change_set.reject", "A change set was rejected by an approver."),
    _a("change_set.remove_op", "An operation was removed from a draft change set."),
    _a("change_set.request_approval", "Approval of a change set was requested."),
    _a("change_set.rollback", "A change set that restores an earlier revision was opened."),
    _a("drift.detected", "An engine's running configuration differs from the desired state."),
    _a("drift.resolved", "An engine's running configuration matches the desired state again."),
    _a("engine.cert_renewed", "An engine agent renewed its client certificate."),
    _a("engine.deployed", "An engine VM was cloned on Proxmox and registered."),
    _a("engine.disabled", "An engine was disabled."),
    _a("engine.enabled", "An engine was enabled."),
    _a("engine.enrolled", "An engine agent enrolled and received a client certificate."),
    _a("engine.enrollment_token_created", "An agent enrollment token was created."),
    _a("engine.register", "An engine was registered."),
    _a("engine.remove", "An engine record was removed."),
    _a("engine.retired", "An engine VM was destroyed and its record removed."),
    _a("gc.run", "Housekeeping removed expired records."),
    _a("idp.created", "An identity provider was created."),
    _a("idp.group_mapped", "A provider group was mapped to a tenant role."),
    _a("job.cancelled", "A queued job was cancelled."),
    _a("job.retried", "A dead job was queued again."),
    _a("pool_member.state", "A pool member was enabled, disabled or drained at runtime."),
    _a("revision.mirrored", "A revision was pushed to the Git mirror."),
    _a("tenant.create", "A tenant was created."),
    _a("tenant.delete", "A tenant was deleted."),
    _a("se_group.reapplied", "The desired state was pushed to an SE group again to repair drift."),
    _a("se_group.reapply_failed", "Re-applying the desired state to an SE group failed."),
    _a("se_group.reapply_requested", "Someone asked to re-apply the desired state to an SE group."),
    _a("tenant.update", "A tenant was changed."),
    _a("auth.session_revoked", "A user's sessions were ended."),
    _a("break_glass.admin_confirmed", "The break-glass authenticator was confirmed."),
    _a("break_glass.admin_created", "The break-glass administrator was enrolled."),
    _a("break_glass.admin_deleted", "The break-glass administrator was removed."),
    _a("break_glass.confirm_failed", "A break-glass enrolment confirmation failed."),
    _a("break_glass.disabled", "Break-glass login was switched off."),
    _a("break_glass.enabled", "Break-glass login was switched on."),
    _a("idp.deleted", "An identity provider was deleted."),
    _a("idp.test_done", "A test sign-in finished."),
    _a("idp.test_started", "A test sign-in was started."),
    _a("idp.updated", "An identity provider was changed."),
    _a("token.system_used", "A system token was used."),
    _a("user.delete", "A user was deleted."),
    _a("user.disable", "A user was disabled."),
    _a("user.enable", "A user was enabled."),
    _a("user.revoke_sessions", "A user's sessions were revoked."),
    _a("token.create", "An API token was created."),
    _a("token.revoke", "An API token was revoked."),
)
