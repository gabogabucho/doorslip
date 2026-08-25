"""Where an agent's key lives (spec §7.3, §10)."""

import json

from doorslip.cli import CONFIG_NAME, agent_home, discover_home, identities


def _configure(root, label, handle="gabo@x"):
    home = root / label
    home.mkdir(parents=True)
    (home / CONFIG_NAME).write_text(
        json.dumps({"server": "https://x", "handle": handle, "label": label}),
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


def test_several_agents_of_one_person_need_no_choosing(tmp_path):
    """This used to return None and it was the wrong rule.

    The old version counted directories. But one identity is one inbox and one
    address book however many agents hold keys to it (spec §7.3), so picking
    between those directories picks which key signs and not who is speaking —
    a question with no wrong answer. It cost a `--home` on every command, and
    it told an agent that four directories were four choices when two of them
    were the same mailbox.
    """
    _configure(tmp_path, "hermes")
    _configure(tmp_path, "claude")

    assert discover_home(tmp_path) == tmp_path / "claude"


def test_two_identities_are_still_not_guessed_between(tmp_path):
    """The question that is real. Accepting an invitation as the wrong
    identity files a stranger in the wrong address book, and the person who
    sent it never reaches who they meant to.
    """
    _configure(tmp_path, "claude", handle="gabo@x")
    _configure(tmp_path, "list", handle="news@x")

    assert discover_home(tmp_path) is None


def test_directories_are_grouped_by_the_handle_they_act_as(tmp_path):
    """What the caller is choosing between, rather than what is on disk."""
    _configure(tmp_path, "claude", handle="gabo@x")
    _configure(tmp_path, "pancho", handle="gabo@x")
    _configure(tmp_path, "list", handle="news@x")

    found = identities(tmp_path)

    assert sorted(found) == ["gabo@x", "news@x"]
    assert [h.name for h in found["gabo@x"]] == ["claude", "pancho"]
    assert [h.name for h in found["news@x"]] == ["list"]


def test_an_unreadable_config_is_not_an_identity(tmp_path):
    """A half-written file must not offer a choice nobody can act on."""
    _configure(tmp_path, "good", handle="gabo@x")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / CONFIG_NAME).write_text("{ not json", encoding="utf-8")

    assert sorted(identities(tmp_path)) == ["gabo@x"]
    assert discover_home(tmp_path) == tmp_path / "good"


def test_nothing_configured_is_not_a_home(tmp_path):
    assert discover_home(tmp_path) is None
    assert discover_home(tmp_path / "missing") is None


def test_a_directory_without_settings_does_not_count(tmp_path):
    """A leftover folder is not an identity."""
    (tmp_path / "leftover").mkdir(parents=True)
    expected = _configure(tmp_path, "hermes")

    assert discover_home(tmp_path) == expected


# -- choosing an identity, which is the only choice that is real ---------


import argparse

import pytest

from doorslip import cli


def _resolve(tmp_path, monkeypatch, **flags):
    monkeypatch.setattr(cli, "DOORSLIP_ROOT", tmp_path)
    return cli._resolve_home(argparse.Namespace(**{"home": None, "as_handle": None, **flags}))


def _refusal(tmp_path, monkeypatch, **flags):
    with pytest.raises(SystemExit) as caught:
        _resolve(tmp_path, monkeypatch, **flags)
    return json.loads(str(caught.value))


def test_as_names_the_identity_and_any_of_its_keys_will_do(tmp_path, monkeypatch):
    _configure(tmp_path, "claude", handle="gabo@x")
    _configure(tmp_path, "pancho", handle="gabo@x")
    _configure(tmp_path, "list", handle="news@x")

    assert _resolve(tmp_path, monkeypatch, as_handle="news@x") == tmp_path / "list"
    assert _resolve(tmp_path, monkeypatch, as_handle="gabo@x").name in ("claude", "pancho")


def test_a_handle_is_matched_however_it_was_typed(tmp_path, monkeypatch):
    """The server folds case on registration, so the CLI cannot be stricter
    than the thing it is naming.
    """
    _configure(tmp_path, "claude", handle="gabo@x")
    _configure(tmp_path, "list", handle="news@x")

    assert _resolve(tmp_path, monkeypatch, as_handle="  NEWS@X ") == tmp_path / "list"


def test_the_refusal_lists_identities_and_not_folder_names(tmp_path, monkeypatch):
    """The bug this replaces.

    A flat list of directories left the caller — often an agent reading this
    as JSON — unable to tell two keys of one mailbox from two different
    people's mailboxes. Those are opposite risks: choosing wrong between the
    first pair costs nothing, and choosing wrong between the second files a
    stranger in the wrong address book.
    """
    _configure(tmp_path, "claude", handle="gabo@x")
    _configure(tmp_path, "pancho", handle="gabo@x")
    _configure(tmp_path, "list", handle="news@x")
    _configure(tmp_path, "pancho-news", handle="news@x")

    refusal = _refusal(tmp_path, monkeypatch)

    assert refusal["identities"] == {
        "gabo@x": ["claude", "pancho"],
        "news@x": ["list", "pancho-news"],
    }
    assert "--as" in refusal["error"]
    assert "--as gabo@x" in refusal["example"]


def test_one_identity_with_several_agents_is_not_a_question(tmp_path, monkeypatch):
    _configure(tmp_path, "hermes", handle="gabo@x")
    _configure(tmp_path, "claude", handle="gabo@x")
    _configure(tmp_path, "pancho", handle="gabo@x")

    assert _resolve(tmp_path, monkeypatch).parent == tmp_path


def test_an_unknown_handle_says_what_is_here(tmp_path, monkeypatch):
    _configure(tmp_path, "claude", handle="gabo@x")
    _configure(tmp_path, "list", handle="news@x")

    refusal = _refusal(tmp_path, monkeypatch, as_handle="nobody@x")

    assert refusal["identities"] == ["gabo@x", "news@x"]


def test_home_still_wins_when_the_key_is_the_point(tmp_path, monkeypatch):
    """Naming a directory stays available: it is how you choose which key
    signs, once you have already decided who is speaking.
    """
    _configure(tmp_path, "claude", handle="gabo@x")
    _configure(tmp_path, "pancho", handle="gabo@x")
    monkeypatch.setattr(cli, "DOORSLIP_ROOT", tmp_path)

    chosen = cli._resolve_home(
        argparse.Namespace(home=str(tmp_path / "pancho"), as_handle=None)
    )

    assert chosen == tmp_path / "pancho"


def test_nothing_configured_says_to_run_setup(tmp_path, monkeypatch):
    refusal = _refusal(tmp_path, monkeypatch)

    assert "setup" in refusal["error"]
