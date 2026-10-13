# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The engine roll state machine (ADR 0205)."""

from switchyard.domain.roll import (
    Deploy,
    Facts,
    IssueToken,
    Phase,
    Reapply,
    Retire,
    Roll,
    Status,
    abort,
    advance,
    pause,
    resume,
)


def drive(roll: Roll, *facts: Facts) -> tuple[Roll, list[object]]:
    cmds: list[object] = []
    for f in facts:
        roll, cmd = advance(roll, f)
        if cmd is not None:
            cmds.append(cmd)
    return roll, cmds


def test_one_engine_goes_through_every_phase_and_the_next_follows() -> None:
    roll = Roll.start(["se-1", "se-2"])
    roll, cmds = drive(
        roll,
        Facts(0),
        Facts(1),  # deploying, nothing yet
        Facts(2, deployed="se-1b"),
        Facts(3),  # awaiting enrollment
        Facts(4, enrolled=True),
        Facts(5, reapplied=True),
        Facts(6, group_score=60),  # not healthy enough yet
        Facts(7, group_score=95),
        Facts(8, retired=True),
        Facts(9, group_score=95),
    )
    assert cmds == [Deploy("se-1"), IssueToken("se-1b"), Reapply(), Retire("se-1")]
    assert roll.steps[0].phase is Phase.DONE and roll.steps[0].new == "se-1b"
    assert roll.current is not None and roll.current.old == "se-2"
    roll, cmds = drive(roll, Facts(10))
    assert cmds == [Deploy("se-2")]


def test_a_command_is_sent_once_per_phase() -> None:
    _, cmds = drive(Roll.start(["a"]), Facts(0), Facts(1), Facts(2), Facts(3))
    assert cmds == [Deploy("a")]


def test_a_failed_deploy_pauses_with_the_old_engine_untouched() -> None:
    roll, cmds = drive(Roll.start(["a"]), Facts(0), Facts(1, deploy_failed=True))
    assert roll.status is Status.PAUSED and "could not be deployed" in roll.reason
    assert cmds == [Deploy("a")] and roll.steps[0].phase is Phase.DEPLOYING


def test_a_failed_smoke_check_pauses_before_the_old_engine_is_retired() -> None:
    roll, cmds = drive(
        Roll.start(["a"]),
        Facts(0),
        Facts(1, deployed="a2"),
        Facts(2, enrolled=True),
        Facts(3, reapplied=True),
        Facts(4, group_score=95, smoke_ok=False),
    )
    assert roll.status is Status.PAUSED and "smoke" in roll.reason
    assert Retire("a") not in cmds


def test_a_step_that_takes_too_long_pauses() -> None:
    roll, _ = drive(Roll.start(["a"]), Facts(0), Facts(1, deployed="a2"), Facts(4000))
    assert roll.status is Status.PAUSED and "awaiting_enrollment" in roll.reason


def test_a_low_engine_score_holds_the_roll() -> None:
    roll, _ = drive(
        Roll.start(["a"]),
        Facts(0),
        Facts(1, deployed="a2"),
        Facts(2, enrolled=True),
        Facts(3, reapplied=True),
        Facts(4, group_score=95, engine_scores_ok=False),
    )
    assert roll.current is not None and roll.current.phase is Phase.SETTLING


def test_pause_resume_and_abort() -> None:
    roll, _ = drive(Roll.start(["a"]), Facts(0))
    paused = pause(roll, "operator")
    assert advance(paused, Facts(1, deployed="a2")) == (paused, None)  # nothing moves while paused
    resumed = resume(paused, 100.0)
    assert resumed.status is Status.RUNNING
    again, cmd = advance(resumed, Facts(101))
    assert cmd == Deploy("a")  # sent again: nothing had been deployed yet
    after, cmd = advance(resume(pause(again, "x"), 200.0), Facts(201, deployed="a2"))
    assert cmd == IssueToken("a2")  # but a finished deploy is not repeated
    stopped = abort(after)
    assert advance(stopped, Facts(300)) == (stopped, None) and stopped.status is Status.ABORTED


def test_an_empty_roll_finishes() -> None:
    roll, _ = advance(Roll.start([]), Facts(0))
    assert roll.status is Status.DONE
