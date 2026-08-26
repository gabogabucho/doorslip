"""What `doorslip setup` does when registration is refused.

Found by reading the seed instance's access log against its database. Of the
agents that fetched the skill, a handful reached `/register`, and `/contacts`
was answering `401` more often than `200` — fifty-seven refusals against
forty-three successes, from agents whose settings existed and whose keys the
server had never seen.

`POST /register` answers `409` for two opposite reasons. `pubkey already
registered` is this key arriving twice, which is a re-run and not a failure.
`handle already registered` is somebody else holding that name. Setup treated
both as benign and wrote the settings either way, so an agent that asked for a
taken handle was told it was fine and then refused on every command it ever
made — with nothing in the output connecting the two.
"""

import json

import pytest
from fastapi.testclient import TestClient

from doorslip import cli
from doorslip.api import create_app
from doorslip.client import Agent, ProtocolError
from doorslip.crypto import generate_keypair
from doorslip.store import Store, connect

SERVER = "doorslip.test"


@pytest.fixture
def http():
    db = connect(":memory:")
    try:
        yield TestClient(create_app(Store(db), welcome_handle=f"welcome@{SERVER}"))
    finally:
        db.close()


def _claim(http, name):
    agent = Agent(
        http, handle=f"{name}@{SERVER}", label="hermes", keypair=generate_keypair()
    )
    agent.register()
    return agent


# -- the server already told them apart ----------------------------------


def test_the_two_conflicts_carry_different_reasons(http):
    """Setup could always have distinguished them; it did not look."""
    first = _claim(http, "popular")

    stranger = Agent(
        http, handle=f"popular@{SERVER}", label="claude", keypair=generate_keypair()
    )
    with pytest.raises(ProtocolError) as taken:
        stranger.register()

    with pytest.raises(ProtocolError) as again:
        Agent(
            http, handle=f"other@{SERVER}", label="hermes", keypair=first._keypair
        ).register()

    assert taken.value.status == again.value.status == 409
    assert "handle already registered" in taken.value.detail
    assert "pubkey already registered" in again.value.detail


def test_a_taken_handle_leaves_an_agent_refused_on_everything(http):
    """The consequence, stated once so the fix has something to point at."""
    _claim(http, "popular")
    stranger = Agent(
        http, handle=f"popular@{SERVER}", label="claude", keypair=generate_keypair()
    )
    with pytest.raises(ProtocolError):
        stranger.register()

    with pytest.raises(ProtocolError) as refused:
        stranger.contacts()

    assert refused.value.status == 401
    assert refused.value.detail == "key is not registered"


# -- what setup does now -------------------------------------------------


def _run_setup(monkeypatch, tmp_path, http, handle, label="hermes"):
    """Run cmd_setup against the test server and capture what it printed."""
    printed = []
    monkeypatch.setattr(cli, "_emit", lambda payload: printed.append(payload))
    monkeypatch.setattr(cli, "DOORSLIP_ROOT", tmp_path)
    # httpx is imported inside the command, so the patch goes on the module.
    monkeypatch.setattr("httpx.Client", lambda **kwargs: http)

    args = cli.build_parser().parse_args(
        ["setup", "--server", f"http://{SERVER}", "--handle", handle, "--label", label]
    )
    code = cli.cmd_setup(args)
    return code, printed[-1] if printed else {}


def test_a_taken_handle_fails_and_writes_no_settings(monkeypatch, tmp_path, http):
    """The whole point. Settings that cannot work must not be left behind
    looking like settings that can.
    """
    _claim(http, "popular")

    code, out = _run_setup(monkeypatch, tmp_path, http, f"popular@{SERVER}", "claude")

    assert code == 1
    assert "handle already registered" in out["error"]
    assert not (tmp_path / "claude" / cli.CONFIG_NAME).exists()


def test_the_refusal_says_what_to_do_about_it(monkeypatch, tmp_path, http):
    """An agent reading this as JSON has to be able to act on it without
    asking anybody what `409` means.
    """
    _claim(http, "popular")

    _, out = _run_setup(monkeypatch, tmp_path, http, f"popular@{SERVER}", "claude")

    assert "first come, first served" in out["why"]
    assert "another handle" in out["do"]
    assert out["handle"] == f"popular@{SERVER}"


def test_the_key_survives_so_a_retry_reuses_it(monkeypatch, tmp_path, http):
    """Burning a keypair per attempt is how one person becomes four abandoned
    keys in the directory's nonce log.
    """
    _claim(http, "popular")
    _run_setup(monkeypatch, tmp_path, http, f"popular@{SERVER}", "claude")

    key_path = tmp_path / "claude" / cli.KEY_NAME
    assert key_path.exists()
    first = json.loads(key_path.read_text(encoding="utf-8"))["public_key"]

    code, out = _run_setup(monkeypatch, tmp_path, http, f"free@{SERVER}", "claude")

    assert code == 0
    assert out["handle"] == f"free@{SERVER}"
    assert json.loads(key_path.read_text(encoding="utf-8"))["public_key"] == first


def test_re_running_setup_with_the_same_key_just_works(monkeypatch, tmp_path, http):
    """This used to come back as a 409 the caller had to forgive.

    Registration is idempotent for its own key now, so a second run is a
    success rather than a conflict somebody has to decide is harmless. That
    decision is what went wrong: forgiving the conflict meant forgiving the
    other one wearing the same status code.

    The assertion that matters is the last one. What setup leaves behind has
    to be able to reach the mailbox, and nothing checked that before.
    """
    assert _run_setup(monkeypatch, tmp_path, http, f"gabo@{SERVER}")[0] == 0

    code, out = _run_setup(monkeypatch, tmp_path, http, f"gabo@{SERVER}")

    assert code == 0
    assert out["registered"] is True
    assert out["handle"] == f"gabo@{SERVER}"

    config = json.loads(
        (tmp_path / "hermes" / cli.CONFIG_NAME).read_text(encoding="utf-8")
    )
    agent = Agent(
        http,
        handle=config["handle"],
        label=config["label"],
        keypair=cli.load_or_create_keypair(tmp_path / "hermes" / cli.KEY_NAME),
    )
    assert agent.contacts() == []


def test_a_configured_agent_can_actually_use_its_mailbox(monkeypatch, tmp_path, http):
    """The check the old code never made: that what setup left behind works."""
    _run_setup(monkeypatch, tmp_path, http, f"gabo@{SERVER}")

    config = json.loads((tmp_path / "hermes" / cli.CONFIG_NAME).read_text(encoding="utf-8"))
    agent = Agent(
        http,
        handle=config["handle"],
        label=config["label"],
        keypair=cli.load_or_create_keypair(tmp_path / "hermes" / cli.KEY_NAME),
    )

    assert agent.contacts() == []


def test_a_revoked_key_cannot_re_register_its_way_back(http):
    """The guard on the idempotent path. Revocation is the one thing a key
    must not be able to undo by asking again, and 'you already own this
    handle' would have been exactly that door.
    """
    owner = _claim(http, "gabo")
    owner.revoke(owner.pubkey)

    with pytest.raises(ProtocolError) as refused:
        Agent(
            http, handle=f"gabo@{SERVER}", label="hermes", keypair=owner._keypair
        ).register()

    assert refused.value.status in (401, 409)
    assert refused.value.detail != f"gabo@{SERVER}"


def test_a_stranger_is_still_refused_after_the_owner_re_runs(http):
    """The narrowing must not have opened the case it was narrowing around."""
    _claim(http, "popular")
    Agent(
        http, handle=f"popular@{SERVER}", label="hermes",
        keypair=generate_keypair(),
    )

    stranger = Agent(
        http, handle=f"popular@{SERVER}", label="claude", keypair=generate_keypair()
    )
    with pytest.raises(ProtocolError) as caught:
        stranger.register()

    assert caught.value.status == 409
    assert "handle already registered" in caught.value.detail
