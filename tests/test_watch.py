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
