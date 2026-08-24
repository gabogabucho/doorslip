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
def test_a_settled_thread_does_not_wake_anybody(status):
    """There is nothing left to agree, so answering would be noise that costs
    the other side inference.
    """
    allowed, reason = should_wake(_summary(status=status), Counter(), DEFAULT_MAX_TURNS)

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

    settled = summarise({"from": "tomas@doorslip.test", "envelope": {"state": {"status": "confirmed"}}})

    assert settled["event"] == "slip"
    assert not should_wake(settled, Counter(), DEFAULT_MAX_TURNS)[0]
