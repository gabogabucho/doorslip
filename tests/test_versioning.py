"""What the server advertises, and what a client does with it."""

import pytest
from fastapi.testclient import TestClient

from doorslip.api import create_app
from doorslip.auth import AUTH_NONCE_ONLY, AUTH_V1, LEGACY_AUTH_REMOVAL_RELEASE
from doorslip.client import Agent
from doorslip.crypto import generate_keypair
from doorslip.envelope import VERSION as ENVELOPE_VERSION
from doorslip.store import Store, connect


@pytest.fixture
def http():
    db = connect(":memory:")
    try:
        yield TestClient(create_app(Store(db)))
    finally:
        db.close()


def test_the_nonce_reply_says_what_the_server_speaks(http):
    """Every authenticated command passes through /nonce, so it is the one
    place a client is guaranteed to look.

    The field exists now precisely because it cannot be added later: a client
    that never learned to read it can never be told it is out of date.
    """
    payload = http.get("/nonce", params={"pubkey": "probe"}).json()

    assert payload["server"]["protocol"] == ENVELOPE_VERSION
    assert payload["server"]["client"]
    assert payload["server"]["skill"].endswith("/skill.md")
    assert payload["server"]["auth"] == [AUTH_V1, AUTH_NONCE_ONLY]
    assert payload["server"]["nonce_only_removal"] == LEGACY_AUTH_REMOVAL_RELEASE


def test_the_legacy_window_has_an_exact_next_release_boundary():
    assert LEGACY_AUTH_REMOVAL_RELEASE == "0.29.0"


def test_the_protocol_version_matches_what_envelopes_carry(http):
    """One string, two places. If they drifted, a receiver deciding how to
    read a message would be consulting a different number than the one the
    message was written with.
    """
    from doorslip.envelope import build, parse

    advertised = http.get("/nonce", params={"pubkey": "probe"}).json()["server"]["protocol"]
    envelope = parse(
        build(
            sender_handle="a@x",
            sender_agent="hermes",
            sender_pubkey="cGs=",
            to="b@x",
            state={},
            prose="hi",
        )
    )

    assert envelope["version"] == advertised


def test_a_client_learns_the_server_details_on_its_first_call(http):
    agent = Agent(http, handle="gabo@doorslip.test", label="hermes", keypair=generate_keypair())

    agent._nonce()

    assert agent.server_info["protocol"] == ENVELOPE_VERSION


def test_no_advice_when_the_client_is_current(http):
    agent = Agent(http, handle="gabo@doorslip.test", label="hermes", keypair=generate_keypair())
    agent._nonce()

    assert agent.update_notice() is None


def test_a_newer_server_release_produces_advice(http):
    agent = Agent(http, handle="gabo@doorslip.test", label="hermes", keypair=generate_keypair())
    agent._nonce()
    agent.server_info = dict(agent.server_info, client="999.0.0")

    notice = agent.update_notice()

    assert notice is not None
    assert notice["available"] == "999.0.0"
    assert notice["skill"].endswith("/skill.md")


def test_an_older_server_release_produces_none(http):
    """A client ahead of the server is not a problem to report. It happens
    while a server is being upgraded and resolves itself.
    """
    agent = Agent(http, handle="gabo@doorslip.test", label="hermes", keypair=generate_keypair())
    agent._nonce()
    agent.server_info = dict(agent.server_info, client="0.0.1")

    assert agent.update_notice() is None


@pytest.mark.parametrize("offered", ["unknown", "", None])
def test_an_unusable_version_gives_no_advice(http, offered):
    """Wrong advice is worse than none: telling somebody to upgrade to a
    version that does not exist sends them looking for a problem elsewhere.
    """
    agent = Agent(http, handle="gabo@doorslip.test", label="hermes", keypair=generate_keypair())
    agent._nonce()
    agent.server_info = dict(agent.server_info, client=offered)

    assert agent.update_notice() is None


def test_advice_never_blocks_the_command(http):
    """Refusing to work over a version number would strand somebody in the
    middle of a conversation for what is usually cosmetic.
    """
    agent = Agent(http, handle="gabo@doorslip.test", label="hermes", keypair=generate_keypair())
    agent.register()
    agent.server_info = dict(agent.server_info, client="999.0.0")

    assert agent.update_notice() is not None
    assert agent.contacts() == []
