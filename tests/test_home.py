"""Where an agent's key lives (spec §7.3, §10)."""

import json

from doorslip.cli import CONFIG_NAME, agent_home, discover_home


def _configure(root, label):
    home = root / label
    home.mkdir(parents=True)
    (home / CONFIG_NAME).write_text(
        json.dumps({"server": "https://x", "handle": "gabo@x", "label": label}),
        encoding="utf-8",
    )
    return home


def test_each_agent_gets_its_own_directory():
    """The identity is shared, the keys are not.

    Same human, same handle, same address book — but one key per agent, which
    is what makes revoking a single agent possible without locking the others
    out of the mailbox.
    """
    assert agent_home("hermes", root_of := __import__("pathlib").Path("/tmp/x")) == root_of / "hermes"
    assert agent_home("claude", root_of) == root_of / "claude"


def test_the_directory_sits_under_the_humans_home_not_the_agents():
    """Uninstalling an agent must not take the mailbox with it.

    There is no identity recovery (spec §10): a key deleted along with some
    program's install directory is a handle lost for good.
    """
    home = agent_home("hermes")

    assert home.name == "hermes"
    assert home.parent.name == ".doorslip"
    assert home.parent.parent == __import__("pathlib").Path.home()


def test_a_single_agent_is_found_without_being_named(tmp_path):
    """Somebody running one agent should never think about this."""
    expected = _configure(tmp_path, "hermes")

    assert discover_home(tmp_path) == expected


def test_several_agents_are_not_guessed_between(tmp_path):
    """Guessing would be worse than asking: acting as the wrong agent signs
    messages with a key the human did not choose.
    """
    _configure(tmp_path, "hermes")
    _configure(tmp_path, "claude")

    assert discover_home(tmp_path) is None


def test_nothing_configured_is_not_a_home(tmp_path):
    assert discover_home(tmp_path) is None
    assert discover_home(tmp_path / "missing") is None


def test_a_directory_without_settings_does_not_count(tmp_path):
    """A leftover folder is not an identity."""
    (tmp_path / "leftover").mkdir(parents=True)
    expected = _configure(tmp_path, "hermes")

    assert discover_home(tmp_path) == expected
