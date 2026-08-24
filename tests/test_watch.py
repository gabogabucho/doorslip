"""Local watcher (client side only — the server has no push endpoint)."""

import pytest

from doorslip.watch import interval_seconds, summarise


def _message(**extra):
    envelope = {
        "state": {"topic": "saturday barbecue", "status": "proposed", "where": "my place"},
        "prose": "Tomas is asking whether Saturday still works.",
        "from": {"handle": "tomas@doorslip.test", "agent": "claude"},
    }
    envelope.update(extra.pop("envelope", {}))
    message = {
        "message_id": "m-1",
        "thread_id": "t-1",
        "from": "tomas@doorslip.test",
        "envelope": envelope,
    }
    message.update(extra)
    return message


def test_a_summary_says_who_wrote_and_what_about():
    summary = summarise(_message())

    assert summary["from"] == "tomas@doorslip.test"
    assert summary["topic"] == "saturday barbecue"
    assert summary["thread_id"] == "t-1"


def test_a_summary_never_carries_the_message_contents():
    """The watcher runs unattended and writes to logs and desktop toasts.

    Putting prose or the body of state there leaks a private conversation into
    places nobody chose. It also takes the decision to read a slip away from
    the human, which belongs to them.
    """
    summary = summarise(_message())

    flattened = str(summary)
    assert "prose" not in summary
    assert "state" not in summary
    assert "Tomas is asking" not in flattened
    assert "my place" not in flattened


def test_a_slip_with_no_recommended_shape_still_summarises():
    """`state` is free. A message that ignores the shape must not break the
    watcher — it just has less to announce.
    """
    summary = summarise(_message(envelope={"state": {"anything": 1}, "prose": "hi"}))

    assert summary["from"] == "tomas@doorslip.test"
    assert summary["topic"] is None


def test_a_malformed_message_does_not_raise():
    summary = summarise({})

    assert summary["event"] == "slip"
    assert summary["from"] is None


@pytest.mark.parametrize(
    ("setting", "seconds"),
    [("15m", 900), ("30m", 1800), ("60m", 3600)],
)
def test_intervals_translate(setting, seconds):
    assert interval_seconds(setting) == seconds


def test_manual_means_do_not_watch():
    """Not an error and not a default to work around: a deliberate choice to
    be left alone, which the watcher must honour by refusing to start.
    """
    assert interval_seconds("manual") is None
    assert interval_seconds("whatever") is None


# -- waking an agent, and the brakes on doing so --------------------------

from collections import Counter

from doorslip.watch import DEFAULT_MAX_TURNS, should_wake


def _summary(**extra):
    base = {
        "from": "tomas@doorslip.test",
        "topic": "barbecue",
        "status": "proposed",
        "thread_id": "t-1",
        "message_id": "m-1",
    }
    base.update(extra)
    return base


def test_an_open_negotiation_wakes_the_agent():
    allowed, _ = should_wake(_summary(), Counter(), DEFAULT_MAX_TURNS)

    assert allowed


@pytest.mark.parametrize("status", ["confirmed", "declined", "cancelled", "done", "CONFIRMED"])
def test_a_thread_both_sides_settled_does_not_wake_anybody(status):
    """There is nothing left to agree, so answering would be noise that costs
    the other side inference.

    Both sides, not one: a single agent declaring the matter closed is making
    a proposal, and stopping on it would let either end the negotiation alone.
    """
    allowed, reason = should_wake(
        _summary(status=status), Counter(), DEFAULT_MAX_TURNS, we_also_settled=True
    )

    assert not allowed
    assert status.lower() in reason


def test_the_turn_ceiling_stops_the_hook():
    """Conversations between people end because people get bored. Two agents
    do not, so the ceiling lives out here where an enthusiastic model cannot
    talk its way past it.
    """
    seen = Counter({"t-1": 3})

    allowed, reason = should_wake(_summary(), seen, 3)

    assert not allowed
    assert "3-turn" in reason


def test_the_ceiling_is_per_thread_not_global():
    seen = Counter({"t-1": 5})

    allowed, _ = should_wake(_summary(thread_id="t-2"), seen, 5)

    assert allowed


def test_refusing_to_wake_never_hides_the_slip():
    """Stopping the automation is not the same as hiding the message. The
    human still has to find out, especially once their agent is no longer
    allowed to answer on its own.
    """
    from doorslip.watch import summarise

    settled = summarise(
        {"from": "tomas@doorslip.test", "envelope": {"state": {"status": "confirmed"}}}
    )

    assert settled["event"] == "slip"
    assert not should_wake(settled, Counter(), DEFAULT_MAX_TURNS, we_also_settled=True)[0]


# -- agreement takes two --------------------------------------------------

from doorslip.watch import settled_by_us, thread_age_hours


def _sent(thread_id, status=None, timestamp=None):
    envelope = {"thread_id": thread_id, "state": {}}
    if status:
        envelope["state"]["status"] = status
    if timestamp:
        envelope["timestamp"] = timestamp
    return envelope


def test_one_side_declaring_it_settled_does_not_stop_the_other():
    """A unilateral `confirmed` is a proposal, not a conclusion. Stopping on
    it would let either agent end a negotiation on its own, and the other
    human would never learn their side was never actually agreed.
    """
    allowed, _ = should_wake(
        _summary(status="confirmed"), Counter(), DEFAULT_MAX_TURNS, we_also_settled=False
    )

    assert allowed


def test_both_sides_settled_ends_it():
    allowed, reason = should_wake(
        _summary(status="confirmed"), Counter(), DEFAULT_MAX_TURNS, we_also_settled=True
    )

    assert not allowed
    assert "both sides" in reason


def test_our_own_terminal_status_is_recognised():
    assert settled_by_us([_sent("t-1", "confirmed")], "t-1")
    assert not settled_by_us([_sent("t-1", "negotiating")], "t-1")
    assert not settled_by_us([_sent("t-2", "confirmed")], "t-1")
    assert not settled_by_us([], "t-1")


def test_a_stale_thread_stops_being_answered():
    """A thread about Saturday still running on Sunday is not coordinating
    anything any more.
    """
    allowed, reason = should_wake(
        _summary(), Counter(), DEFAULT_MAX_TURNS, thread_age_hours=72.0, max_age_hours=48.0
    )

    assert not allowed
    assert "older than" in reason


def test_a_fresh_thread_is_not_stale():
    allowed, _ = should_wake(
        _summary(), Counter(), DEFAULT_MAX_TURNS, thread_age_hours=2.0, max_age_hours=48.0
    )

    assert allowed


def test_thread_age_comes_from_the_oldest_message_seen():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    envelopes = [
        _sent("t-1", timestamp=(now - timedelta(hours=5)).isoformat()),
        _sent("t-1", timestamp=(now - timedelta(hours=1)).isoformat()),
        _sent("t-2", timestamp=(now - timedelta(hours=90)).isoformat()),
    ]

    assert 4.9 < thread_age_hours(envelopes, "t-1") < 5.1


def test_an_unknown_thread_has_no_age_and_is_not_blocked():
    """No information is not evidence of staleness. Guessing old would stop
    perfectly live conversations on a machine that just restarted.
    """
    assert thread_age_hours([], "t-1") is None

    allowed, _ = should_wake(
        _summary(), Counter(), DEFAULT_MAX_TURNS, thread_age_hours=None, max_age_hours=48.0
    )
    assert allowed
