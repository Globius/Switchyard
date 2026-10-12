# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Enumerations for change sets, deployments, jobs, engines and API access."""

import enum


class ChangeSetState(enum.StrEnum):
    """Lifecycle of a change set (backend design, ``change_set.state``)."""

    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPLYING = "applying"
    APPLIED = "applied"
    FAILED = "failed"
    DISCARDED = "discarded"


class DeploymentState(enum.StrEnum):
    """Outcome of pushing one revision to one SE group."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REVERTED = "reverted"


class JobState(enum.StrEnum):
    """Lifecycle of a background job."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"
    CANCELLED = "cancelled"


class EngineState(enum.StrEnum):
    """Whether a service engine takes part in rollouts."""

    ENABLED = "enabled"
    DISABLED = "disabled"


class Role(enum.StrEnum):
    """Roles from the backend design ("Auth and RBAC"), weakest first."""

    VIEWER = "viewer"
    OPERATOR = "operator"
    TENANT_ADMIN = "tenant-admin"
    SYSTEM_ADMIN = "system-admin"
