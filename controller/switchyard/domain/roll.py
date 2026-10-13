# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The engine roll: replace every engine of an SE group with one from a new template (ADR 0205).

Pure state machine, no I/O. One engine at a time, and for each: deploy a replacement, wait for it to
enroll (the one step that needs an operator or the guest cloud-init; ADR 0083), reapply the group so
the replacement gets the current configuration, wait for the group's health score and a smoke
check, retire the old engine, and wait for the score again. A step that fails or times out
**pauses** the roll with both engines still in place, which is the canary and the rollback point:
an operator resumes, or aborts, and the old engine keeps serving.

Example:
    >>> roll = Roll.start(["se-1", "se-2"])
    >>> roll, cmd = advance(roll, Facts(now=0.0))
    >>> cmd
    Deploy(replaces='se-1')
"""

from dataclasses import dataclass, field, replace
from enum import StrEnum

#: Health score a group must reach before the roll moves on (ADR 0083).
MIN_SCORE = 80
#: Seconds each phase may take before the roll pauses.
TIMEOUTS: dict[str, float] = {
    "deploying": 900.0,
    "awaiting_enrollment": 3600.0,
    "reapplying": 600.0,
    "settling": 600.0,
    "retiring": 600.0,
    "settling_after": 600.0,
}


class Phase(StrEnum):
    """Where the current engine is."""

    PENDING = "pending"
    DEPLOYING = "deploying"
    AWAITING_ENROLLMENT = "awaiting_enrollment"
    REAPPLYING = "reapplying"
    SETTLING = "settling"
    RETIRING = "retiring"
    SETTLING_AFTER = "settling_after"
    DONE = "done"


class Status(StrEnum):
    """Where the roll is."""

    RUNNING = "running"
    PAUSED = "paused"
    ABORTED = "aborted"
    DONE = "done"


@dataclass(frozen=True)
class Deploy:
    """Create a replacement for ``replaces`` from the new template."""

    replaces: str


@dataclass(frozen=True)
class IssueToken:
    """Issue an enrollment token for the replacement ``engine``."""

    engine: str


@dataclass(frozen=True)
class Reapply:
    """Reapply the group's configuration (a canary-first apply, no new revision)."""


@dataclass(frozen=True)
class Retire:
    """Retire the old ``engine``."""

    engine: str


Command = Deploy | IssueToken | Reapply | Retire


@dataclass(frozen=True)
class Facts:
    """What the world looks like now. The caller fills in only what it has looked up.

    Attributes:
        now: A monotonic clock, seconds.
        deployed: Engine id of the replacement once its deploy job has finished, else ``None``.
        deploy_failed: The deploy job failed.
        enrolled: The replacement is reachable and its agent is connected.
        reapplied: The reapply job has finished.
        group_score: The group's health score (0 to 100), or ``None`` when unknown.
        engine_scores_ok: Every engine meets the minimum score.
        smoke_ok: The smoke check passed (login, a read-only call, the VIP answers, no new drift).
        retired: The old engine is gone.
    """

    now: float
    deployed: str | None = None
    deploy_failed: bool = False
    enrolled: bool = False
    reapplied: bool = False
    group_score: int | None = None
    engine_scores_ok: bool = True
    smoke_ok: bool = True
    retired: bool = False


@dataclass(frozen=True)
class EngineStep:
    """One engine to replace."""

    old: str
    new: str | None = None
    phase: Phase = Phase.PENDING
    entered_at: float = 0.0
    command_sent: bool = False


@dataclass(frozen=True)
class Roll:
    """A roll over one SE group."""

    steps: tuple[EngineStep, ...]
    status: Status = Status.RUNNING
    reason: str = ""
    log: tuple[str, ...] = field(default=())

    @classmethod
    def start(cls, engines: list[str]) -> "Roll":
        """A new roll over ``engines``, in the order given (a VRRP master goes at its turn)."""
        return cls(tuple(EngineStep(e) for e in engines))

    @property
    def current(self) -> EngineStep | None:
        """The step in progress, or the next one to start; ``None`` when all are done."""
        return next((s for s in self.steps if s.phase is not Phase.DONE), None)


def pause(roll: Roll, reason: str) -> Roll:
    """Stop advancing. Both engines stay as they are."""
    return replace(roll, status=Status.PAUSED, reason=reason, log=(*roll.log, f"paused: {reason}"))


def resume(roll: Roll, now: float) -> Roll:
    """Continue after a pause. The current phase's clock restarts and its command is sent again."""
    cur = roll.current
    if roll.status is not Status.PAUSED or cur is None:
        return roll
    return _put(
        replace(roll, status=Status.RUNNING, reason="", log=(*roll.log, "resumed")),
        replace(cur, entered_at=now, command_sent=False),
    )


def abort(roll: Roll) -> Roll:
    """End the roll. Engines already replaced stay replaced; the old engine keeps serving."""
    return replace(roll, status=Status.ABORTED, log=(*roll.log, "aborted"))


def _put(roll: Roll, step: EngineStep) -> Roll:
    return replace(roll, steps=tuple(step if s.old == step.old else s for s in roll.steps))


def _enter(roll: Roll, step: EngineStep, phase: Phase, now: float, note: str) -> Roll:
    return _put(
        replace(roll, log=(*roll.log, note)),
        replace(step, phase=phase, entered_at=now, command_sent=False),
    )


def advance(roll: Roll, facts: Facts) -> tuple[Roll, Command | None]:
    """One tick: returns the new roll and a command to carry out (at most one), or ``None``.

    A command is returned once per phase entry; the caller runs it and keeps calling
    ``advance`` with fresh facts. Nothing happens while the roll is paused, aborted or done.
    """
    if roll.status is not Status.RUNNING:
        return roll, None
    step = roll.current
    if step is None:
        return replace(roll, status=Status.DONE, log=(*roll.log, "done")), None
    now = facts.now
    limit = TIMEOUTS.get(step.phase.value)
    if limit is not None and now - step.entered_at > limit:
        return pause(roll, f"{step.old}: {step.phase.value} took longer than {limit:.0f}s"), None

    def send(r: Roll, s: EngineStep, cmd: Command) -> tuple[Roll, Command]:
        return _put(r, replace(s, command_sent=True)), cmd

    match step.phase:
        case Phase.PENDING:
            roll = _enter(roll, step, Phase.DEPLOYING, now, f"{step.old}: deploying replacement")
            return send(roll, roll.current or step, Deploy(step.old))
        case Phase.DEPLOYING:
            if facts.deploy_failed:
                return pause(roll, f"{step.old}: the replacement could not be deployed"), None
            if facts.deployed:
                step = replace(step, new=facts.deployed)
                roll = _enter(
                    _put(roll, step),
                    step,
                    Phase.AWAITING_ENROLLMENT,
                    now,
                    f"{facts.deployed}: deployed",
                )
                return send(roll, roll.current or step, IssueToken(facts.deployed))
        case Phase.AWAITING_ENROLLMENT:
            if facts.enrolled:
                roll = _enter(roll, step, Phase.REAPPLYING, now, f"{step.new}: enrolled")
                return send(roll, roll.current or step, Reapply())
        case Phase.REAPPLYING:
            if facts.reapplied:
                return _enter(roll, step, Phase.SETTLING, now, "configuration applied"), None
        case Phase.SETTLING:
            if _settled(facts):
                if not facts.smoke_ok:
                    return pause(roll, f"{step.new}: the smoke check failed"), None
                roll = _enter(roll, step, Phase.RETIRING, now, f"{step.old}: retiring")
                return send(roll, roll.current or step, Retire(step.old))
        case Phase.RETIRING:
            if facts.retired:
                return _enter(roll, step, Phase.SETTLING_AFTER, now, f"{step.old}: retired"), None
        case Phase.SETTLING_AFTER:
            if _settled(facts):
                roll = _enter(roll, step, Phase.DONE, now, f"{step.old}: replaced by {step.new}")
                return roll, None
        case Phase.DONE:  # pragma: no cover - current skips done steps
            return roll, None
    # No progress this tick. After a resume the phase's command was cleared: send it again.
    if not step.command_sent:
        again = _command_for(step)
        if again is not None:
            return send(roll, step, again)
    return roll, None


def _settled(facts: Facts) -> bool:
    return (
        facts.group_score is not None and facts.group_score >= MIN_SCORE and facts.engine_scores_ok
    )


def _command_for(step: EngineStep) -> Command | None:
    match step.phase:
        case Phase.DEPLOYING:
            return Deploy(step.old)
        case Phase.AWAITING_ENROLLMENT if step.new:
            return IssueToken(step.new)
        case Phase.REAPPLYING:
            return Reapply()
        case Phase.RETIRING:
            return Retire(step.old)
    return None


def to_json(roll: Roll) -> dict[str, object]:
    """Plain JSON form of a roll, for the database.

    Example:
        >>> from_json(to_json(Roll.start(["a"]))) == Roll.start(["a"])
        True
    """
    return {
        "status": roll.status.value,
        "reason": roll.reason,
        "log": list(roll.log),
        "steps": [
            {
                "old": s.old,
                "new": s.new,
                "phase": s.phase.value,
                "entered_at": s.entered_at,
                "command_sent": s.command_sent,
            }
            for s in roll.steps
        ],
    }


def from_json(data: dict[str, object]) -> Roll:
    """Inverse of :func:`to_json`."""
    steps = tuple(
        EngineStep(
            old=str(s["old"]),
            new=None if s["new"] is None else str(s["new"]),
            phase=Phase(str(s["phase"])),
            entered_at=float(s["entered_at"]),
            command_sent=bool(s["command_sent"]),
        )
        for s in data["steps"]  # type: ignore[attr-defined]
    )
    return Roll(
        steps,
        Status(str(data["status"])),
        str(data["reason"]),
        tuple(str(x) for x in data["log"]),  # type: ignore[attr-defined]
    )
