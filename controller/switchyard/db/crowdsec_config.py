# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The stored CrowdSec integration settings (ADR 0213), readable from every layer.

They live in ``system_setting`` under the key ``crowdsec``; ``SWITCHYARD_CROWDSEC_URL``,
``_API_KEY`` and ``_ORIGINS`` are the fallback while nothing is stored. The bouncer key is never
stored here, only ``key_ref``, its reference in the secrets backend.

Example:
    >>> normalize_origins(" crowdsec, CAPI ,,")
    ['crowdsec', 'CAPI']
"""

import os
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from switchyard.db.auth_models import SystemSettingRow
from switchyard.db.tenancy import admin_session

CONFIG_KEY = "crowdsec"
#: Where an engine's own bouncer key lives when ``per_engine_keys`` is on (``{engine}``: its name).
ENGINE_KEY_REF = "kv/system/crowdsec/bouncer-keys/{engine}"


def normalize_origins(value: str | list[str] | None) -> list[str]:
    """A comma-separated string or a list as a clean list of origin names.

    Example:
        >>> normalize_origins(["CAPI", " crowdsec "]), normalize_origins(None)
        (['CAPI', 'crowdsec'], [])
    """
    items = value.split(",") if isinstance(value, str) else (value or [])
    return [o.strip() for o in items if o and o.strip()]


@dataclass(frozen=True)
class CrowdSecConfig:
    """The effective settings.

    Attributes:
        url: Base URL of the CrowdSec local API; empty when not configured.
        key_ref: Reference of the bouncer key in the secrets backend; empty when it comes from the
            environment or is not set.
        origins: Keep only decisions from these origins; empty keeps all.
        logs_address: ``host:port`` (UDP) of the CrowdSec syslog datasource that receives the
            access logs of services that opt in; empty turns the feed off.
        per_engine_keys: Each engine uses its own bouncer key (:data:`ENGINE_KEY_REF`) instead of
            the shared one.
        source: ``database``, ``environment`` or ``none``.
    """

    url: str = ""
    key_ref: str = ""
    origins: list[str] = field(default_factory=list)
    source: str = "none"
    logs_address: str = ""
    per_engine_keys: bool = False

    @property
    def configured(self) -> bool:
        """True when there is a URL."""
        return bool(self.url)


async def get_setting(engine: AsyncEngine, key: str) -> Any:
    """The JSON value of a ``system_setting`` row, or ``None``."""
    async with admin_session(engine) as session:
        row = (
            await session.execute(select(SystemSettingRow).where(SystemSettingRow.key == key))
        ).scalar_one_or_none()
    return None if row is None else row.value


async def load_config(engine: AsyncEngine) -> CrowdSecConfig:
    """The stored settings, else the environment's, else an unconfigured result."""
    raw = await get_setting(engine, CONFIG_KEY)
    if raw and raw.get("url"):
        return CrowdSecConfig(
            raw["url"],
            raw.get("key_ref", ""),
            list(raw.get("origins", [])),
            "database",
            raw.get("logs_address", ""),
            bool(raw.get("per_engine_keys", False)),
        )
    env_url = os.environ.get("SWITCHYARD_CROWDSEC_URL") or ""
    if env_url:
        return CrowdSecConfig(
            env_url,
            "",
            normalize_origins(os.environ.get("SWITCHYARD_CROWDSEC_ORIGINS")),
            "environment",
            os.environ.get("SWITCHYARD_CROWDSEC_LOGS_ADDRESS", ""),
        )
    return CrowdSecConfig()


@dataclass(frozen=True)
class StoredBouncer:
    """What building an engine's bundle needs from the settings (the key is read later).

    Attributes:
        url: Base URL of the CrowdSec local API.
        key_ref: Reference of the bouncer key in the secrets backend, or empty.
        env_key: The bouncer key from the environment when there is no reference.
        origins: Decision origins to keep; empty keeps all.
        logs_address: ``host:port`` (UDP) for the access log feed, or empty.
        per_engine_keys: Each engine reads its own key from :data:`ENGINE_KEY_REF`.
    """

    url: str
    key_ref: str
    env_key: str
    origins: tuple[str, ...]
    logs_address: str
    per_engine_keys: bool = False


async def bouncer_settings(engine: AsyncEngine) -> StoredBouncer | None:
    """What an engine's bouncer needs, or ``None`` when CrowdSec is not configured.

    Used when the agent bundle is built; the key itself is read from the secrets backend there.
    """
    cfg = await load_config(engine)
    if not cfg.configured:
        return None
    env_key = "" if cfg.key_ref else os.environ.get("SWITCHYARD_CROWDSEC_API_KEY", "")
    return StoredBouncer(
        cfg.url, cfg.key_ref, env_key, tuple(cfg.origins), cfg.logs_address, cfg.per_engine_keys
    )
