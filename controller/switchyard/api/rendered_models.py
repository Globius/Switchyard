# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Response models for rendered configuration: the per-object fragments and the change-set diff."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class RenderedFragment(BaseModel):
    """Part of one rendered file that an object produced."""

    se_group_id: UUID = Field(description="The SE group whose files contain this text.")
    file: str = Field(description="Path relative to /etc/switchyard/ on the SE.")
    language: Literal["haproxy", "plaintext"] = Field(description="Editor language id.")
    content: str = Field(description="The whole file; highlight `range` inside it.")
    fingerprint: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="Render fingerprint of the SE group (what an SE reports it is running).",
    )
    range: tuple[int, int] | None = Field(
        default=None,
        description="First and last line (1-based, inclusive) of the fragment inside `content`; "
        "null when the fragment is the whole file.",
    )


class RenderedConfig(BaseModel):
    """Rendered fragments of one object, live or with a change set applied."""

    items: list[RenderedFragment] = Field(
        description="Empty when the object renders nothing (unused pool, disabled or UDP service)."
    )
