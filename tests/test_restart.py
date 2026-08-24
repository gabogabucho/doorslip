"""The welcome desk across a restart (spec §8).

Every other test starts from an empty database, which is exactly why this
failure hid: the desk only breaks the *second* time a server comes up against
data that is already there.
"""

import pytest
from fastapi.testclient import TestClient

from doorslip.api import create_app
from doorslip.client import Agent
from doorslip.crypto import generate_keypair
from doorslip.store import Store, connect

WELCOME = "welcome@doorslip.test"


@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path / "doorslip.db")
    try:
        yield connection
    finally:
        connection.close()


def _boot(db, tmp_path):
    """Bring a server up against whatever is already stored."""
    return TestClient(
        create_app(
            Store(db),
            welcome_handle=WELCOME,
            welcome_key_path=tmp_path / "welcome.json",
        )
    )


def _agent(http, handle, label="hermes"):
    agent = Agent(http, handle=handle, label=label, keypair=generate_keypair())
    agent.register()
    return agent


def test_the_desk_keeps_its_key_across_a_restart(db, tmp_path):
    """A fresh key each boot is one the `agent` table has never seen, so every
    notice the desk tries to send fails the lookup and is dropped in silence.
    """
    first = create_app(Store(db), welcome_handle=WELCOME, welcome_key_path=tmp_path / "w.json")
    second = create_app(Store(db), welcome_handle=WELCOME, welcome_key_path=tmp_path / "w.json")

    assert first.state.welcome.keypair.public_key == second.state.welcome.keypair.public_key


def test_the_desk_still_answers_after_a_restart(db, tmp_path):
    http = _boot(db, tmp_path)
    gabo = _agent(http, "gabo@doorslip.test")

    restarted = _boot(db, tmp_path)
    gabo._http = restarted
    gabo.send(to=WELCOME, state={"topic": "hello"}, prose="still there?")

    assert any(m["from"] == WELCOME for m in gabo.inbox())


def test_an_acceptance_notice_survives_a_restart(db, tmp_path):
    """The case that surfaced this: a server redeployed without wiping its
    database stopped telling anybody their invitation had been accepted.
    """
    http = _boot(db, tmp_path)
    gabo = _agent(http, "gabo@doorslip.test")
    tomas = _agent(http, "tomas@doorslip.test", label="claude")
    code = gabo.invite()

    restarted = _boot(db, tmp_path)
    gabo._http = restarted
    tomas._http = restarted
    tomas.accept(code)

    topics = [m["envelope"]["state"].get("topic") for m in gabo.inbox()]
    assert "an invitation you sent was accepted" in topics


def test_a_lost_key_file_is_recovered_rather_than_failing_mutely(db, tmp_path):
    """If the key file is gone the identity still exists, so a new key is
    attached instead of leaving the desk unable to sign anything.
    """
    http = _boot(db, tmp_path)
    gabo = _agent(http, "gabo@doorslip.test")
    (tmp_path / "welcome.json").unlink()

    restarted = _boot(db, tmp_path)
    gabo._http = restarted
    gabo.send(to=WELCOME, state={"topic": "hello"}, prose="after losing the key")

    assert any(m["from"] == WELCOME for m in gabo.inbox())
