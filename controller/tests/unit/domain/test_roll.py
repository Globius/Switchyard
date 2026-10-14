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
        Facts(9, reapplied=True),  # the retirement changed the group: applied again
        Facts(10, group_score=95),
    )
    assert cmds == [Deploy("se-1"), IssueToken("se-1b"), Reapply(), Retire("se-1"), Reapply()]
    assert roll.steps[0].phase is Phase.DONE and roll.steps[0].new == "se-1b"
    assert roll.current is not None and roll.current.old == "se-2"
    roll, cmds = drive(roll, Facts(11))
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


def _at(day: int, hour: int, minute: int = 0) -> float:
    """Epoch seconds for 2026-10-12 + ``day`` (a Monday is day 0) at hour:minute UTC."""
    from datetime import UTC, datetime

    return datetime(2026, 10, 12 + day, hour, minute, tzinfo=UTC).timestamp()


def test_windows_cross_midnight_and_pick_weekdays() -> None:
    from switchyard.domain.roll import Window, in_window

    night = Window(22 * 60, 4 * 60)  # 22:00-04:00
    assert in_window(night, _at(0, 23)) and in_window(night, _at(1, 3))
    assert not in_window(night, _at(1, 4)) and not in_window(night, _at(0, 12))
    assert in_window(None, _at(0, 12))
    weekend = Window(0, 24 * 60 - 1, days=(5, 6))
    assert in_window(weekend, _at(5, 10)) and not in_window(weekend, _at(0, 10))
    # after midnight the window belongs to the day it began: Friday night runs into Saturday
    fri_night = Window(22 * 60, 4 * 60, days=(4,))
    assert in_window(fri_night, _at(5, 2)) and not in_window(fri_night, _at(6, 2))
    assert night.label() == "22:00-04:00 UTC"


def test_a_roll_waits_for_its_window_and_not_before() -> None:
    from switchyard.domain.roll import Window

    roll = Roll.start(["a"], window=Window(2 * 60, 5 * 60), not_before=_at(1, 0))
    roll, cmd = advance(roll, Facts(_at(0, 3)))  # in the window, but before the start time
    assert cmd is None and roll.holding and "scheduled start" in roll.reason
    roll, cmd = advance(roll, Facts(_at(1, 12)))  # after the start, outside the window
    assert cmd is None and "window 02:00-05:00 UTC" in roll.reason
    log_length = len(roll.log)
    roll, cmd = advance(roll, Facts(_at(1, 13)))
    assert len(roll.log) == log_length  # holding does not spam the log
    roll, cmd = advance(roll, Facts(_at(2, 3)))
    assert cmd == Deploy("a") and not roll.holding and roll.reason == ""


def test_a_step_in_progress_is_not_interrupted_when_the_window_closes() -> None:
    from switchyard.domain.roll import Window

    roll = Roll.start(["a", "b"], window=Window(2 * 60, 5 * 60))
    roll, _ = drive(
        roll,
        Facts(_at(0, 4, 50)),
        Facts(_at(0, 4, 51), deployed="a2"),
        Facts(_at(0, 5, 10), enrolled=True),  # window closed at 05:00; this engine finishes
        Facts(_at(0, 5, 11), reapplied=True),
        Facts(_at(0, 5, 12), group_score=95),
        Facts(_at(0, 5, 13), retired=True),
        Facts(_at(0, 5, 14), reapplied=True),
        Facts(_at(0, 5, 15), group_score=95),
    )
    assert roll.steps[0].phase is Phase.DONE
    roll, cmd = advance(roll, Facts(_at(0, 5, 16)))
    assert cmd is None and "window" in roll.reason  # b waits for tomorrow night


def test_soak_time_separates_engines() -> None:
    roll = Roll.start(["a", "b"], soak_s=600.0)
    roll, _ = drive(
        roll,
        Facts(0),
        Facts(1, deployed="a2"),
        Facts(2, enrolled=True),
        Facts(3, reapplied=True),
        Facts(4, group_score=95),
        Facts(5, retired=True),
        Facts(6, reapplied=True),
        Facts(7, group_score=95),  # a is done at t=7
    )
    roll, cmd = advance(roll, Facts(300))
    assert cmd is None and "soaking" in roll.reason
    roll, cmd = advance(roll, Facts(608))
    assert cmd == Deploy("b")


def test_schedule_survives_storage() -> None:
    from switchyard.domain.roll import Window, from_json, to_json

    roll = Roll.start(["a"], window=Window(60, 120, (1, 2)), not_before=5.0, soak_s=30.0)
    assert from_json(to_json(roll)) == roll
    old = to_json(Roll.start(["a"]))
    for key in ("window", "not_before", "soak_s", "holding"):
        old.pop(key)
    assert from_json(old) == Roll.start(["a"])  # rolls stored before schedules existed


def test_a_reapply_that_did_not_apply_pauses_instead_of_carrying_on() -> None:
    """The lab: a reapply job finished 'succeeded' with a ReadTimeout error and the roll went on."""
    roll, cmds = drive(
        Roll.start(["a"]),
        Facts(0),
        Facts(1, deployed="a2"),
        Facts(2, enrolled=True),
        Facts(3, reapply_error="a2: ReadTimeout"),
    )
    assert roll.status is Status.PAUSED and "could not be reapplied" in roll.reason
    assert roll.steps[0].phase is Phase.REAPPLYING and cmds[-1] == Reapply()
    roll, cmds = drive(resume(roll, 4.0), Facts(5), Facts(6, reapplied=True))
    assert roll.steps[0].phase is Phase.SETTLING and cmds == [Reapply()]  # the retry was sent
