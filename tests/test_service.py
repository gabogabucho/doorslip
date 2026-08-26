"""Keeping the watcher alive across reboots.

From the same feedback that produced `doorslip status`: there was no simple
way to keep the watcher running. There still is no protocol answer to that —
it is a local process — but leaving every person to invent the same unit file
meant most of them did not, and a watcher that is not running is somebody who
stops hearing from anyone and is never told.

The two properties worth defending are that the definition pins an identity,
and that writing one does not start one.
"""

import sys

import pytest

from doorslip import service


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(service.Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


HANDLE = "gabo@doorslip.org"


# -- the identity is pinned ----------------------------------------------


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_every_definition_names_the_identity(monkeypatch, platform):
    """A watcher that relied on discovery works until a second mailbox is set
    up on this machine, and then the CLI correctly refuses to guess and the
    service dies without saying anything. Naming the handle now costs a flag
    and removes that failure entirely.
    """
    monkeypatch.setattr(service.sys, "platform", platform)

    made = service.plan(HANDLE)

    body = str(made.get("contents") or "") + " ".join(made["enable"])
    assert "--as" in body
    assert HANDLE in body


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_it_invokes_the_interpreter_that_is_running(monkeypatch, platform):
    """Not whatever `doorslip` resolves to on PATH. A console script from a
    virtualenv or from uvx may not be on the PATH a service manager builds.
    """
    monkeypatch.setattr(service.sys, "platform", platform)

    contents = str(service.plan(HANDLE)["contents"])

    assert sys.executable in contents
    assert "-m" in contents and "doorslip" in contents


def test_the_flags_of_this_run_reach_the_service(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")

    contents = str(service.plan(HANDLE, ["--every", "15m", "--quiet"])["contents"])

    assert "--every 15m" in contents
    assert "--quiet" in contents


# -- restarting is the point ----------------------------------------------


def test_systemd_restarts_it(monkeypatch):
    """A watcher that stays down after one bad night is the failure this file
    exists to prevent, not a tidiness preference.
    """
    monkeypatch.setattr(service.sys, "platform", "linux")

    contents = str(service.plan(HANDLE)["contents"])

    assert "Restart=always" in contents


def test_launchd_keeps_it_alive(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "darwin")

    contents = str(service.plan(HANDLE)["contents"])

    assert "<key>KeepAlive</key>" in contents
    assert "<key>RunAtLoad</key>" in contents


def test_windows_says_plainly_that_it_does_not_restart(monkeypatch):
    """Task Scheduler starts it at logon and does not bring it back if it
    exits. Saying so beats letting somebody find out.
    """
    monkeypatch.setattr(service.sys, "platform", "win32")

    made = service.plan(HANDLE)

    assert "does not restart" in made["note"]
    assert made["path"] is None


# -- writing is not starting ----------------------------------------------


def test_install_writes_the_file_and_starts_nothing(monkeypatch, home):
    monkeypatch.setattr(service.sys, "platform", "linux")

    result = service.install(HANDLE)

    written = home / ".config" / "systemd" / "user" / "doorslip-watch.service"
    assert written.exists()
    assert result["wrote"] == str(written)
    assert "your decision" in result["not_enabled"]
    assert any("enable" in step for step in result["now_run"])


def test_install_says_how_to_stop_it_too(monkeypatch):
    """Anything that tells you how to start a background process and not how
    to stop it has given you a problem rather than a feature.
    """
    monkeypatch.setattr(service.sys, "platform", "linux")

    result = service.install(HANDLE)

    assert result["to_stop"]
    assert result["logs"]


def test_linux_mentions_lingering(monkeypatch):
    """A user service stops at logout unless lingering is on, and somebody who
    asked for a watcher did not mean "while I am logged in".
    """
    monkeypatch.setattr(service.sys, "platform", "linux")

    assert "enable-linger" in service.plan(HANDLE)["note"]


def test_uninstall_removes_it(monkeypatch, home):
    monkeypatch.setattr(service.sys, "platform", "linux")
    service.install(HANDLE)

    result = service.uninstall(HANDLE)

    written = home / ".config" / "systemd" / "user" / "doorslip-watch.service"
    assert not written.exists()
    assert result["removed"] == str(written)


def test_uninstalling_what_is_not_there_is_not_an_error(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")

    result = service.uninstall(HANDLE)

    assert result["removed"] is None
    assert "error" not in result


# -- an unknown platform says so -----------------------------------------


def test_an_unsupported_platform_refuses_rather_than_guesses(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "sunos5")

    made = service.plan(HANDLE)

    assert "sunos5" in made["error"]
    assert "doorslip watch" in made["do"]


def test_the_windows_command_is_quoted_so_it_can_be_pasted(monkeypatch):
    """The first version quoted every argument and produced nested bare quotes
    that schtasks rejects — a command that looks fine in a JSON blob and fails
    the moment somebody runs it.
    """
    monkeypatch.setattr(service.sys, "platform", "win32")

    command = service.plan(HANDLE, ["--quiet"])["enable"][0]

    assert command.count('"') % 2 == 0
    assert '""' not in command
    assert '\\"' in command          # the interpreter path, escaped for cmd
    assert '"-m"' not in command     # bare arguments stay bare
    assert command.endswith("/f")
    assert "--quiet" in command
