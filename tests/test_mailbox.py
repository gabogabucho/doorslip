"""Address book and mailbox, end to end over HTTP (spec §7.4, §7.5, §7.7, §8)."""

import pytest
from fastapi.testclient import TestClient

from doorslip.api import SIGNATURE_HEADER, create_app
from doorslip.client import Agent, ProtocolError
from doorslip.crypto import generate_keypair
from doorslip.envelope import build, seal
from doorslip.store import Store, connect

WELCOME = "welcome@doorslip.test"


@pytest.fixture
def http():
    db = connect(":memory:")
    try:
        yield TestClient(create_app(Store(db), welcome_handle=WELCOME))
    finally:
        db.close()


@pytest.fixture
def gabo(http):
    agent = Agent(
        http, handle="gabo@doorslip.test", label="hermes", keypair=generate_keypair()
    )
    agent.register()
    return agent


@pytest.fixture
def tomas(http):
    agent = Agent(
        http, handle="tomas@doorslip.test", label="claude", keypair=generate_keypair()
    )
    agent.register()
    return agent


def _introduce(gabo, tomas):
    tomas.accept(gabo.invite())


# -- address book ---------------------------------------------------------


def test_accepting_an_invitation_creates_both_sides(gabo, tomas):
    """The address book is symmetric: a one-sided one would let the inviter
    write while the invitee could not reply.
    """
    _introduce(gabo, tomas)

    assert tomas.handle in gabo.contacts()
    assert gabo.handle in tomas.contacts()


def test_an_invitation_can_only_be_redeemed_once(gabo, tomas, http):
    code = gabo.invite()
    tomas.accept(code)
    third = Agent(
        http, handle="nanton@doorslip.test", label="hermes", keypair=generate_keypair()
    )
    third.register()

    with pytest.raises(ProtocolError) as caught:
        third.accept(code)

    assert caught.value.status == 400


def test_you_cannot_redeem_your_own_invitation(gabo):
    with pytest.raises(ProtocolError):
        gabo.accept(gabo.invite())


def test_an_enrolment_code_is_refused_by_accept(gabo):
    """The two prefixes exist so this exact mistake is catchable.

    An enrolment code attaches a key to YOUR identity; accepting one here
    would mean adding a stranger as your own agent.
    """
    with pytest.raises(ProtocolError) as caught:
        gabo.accept("ds_enr_something")

    assert caught.value.status == 400


# -- the mailbox ----------------------------------------------------------


def test_a_message_between_contacts_arrives(gabo, tomas):
    _introduce(gabo, tomas)

    gabo.send(to=tomas.handle, state={"topic": "barbecue"}, prose="Saturday?")

    received = tomas.inbox()
    assert len(received) == 1
    assert received[0]["from"] == gabo.handle
    assert received[0]["envelope"]["prose"] == "Saturday?"


def test_a_stranger_is_refused(gabo, tomas):
    """The address book IS the anti-spam of v0, and it is enough."""
    with pytest.raises(ProtocolError) as caught:
        tomas.send(to=gabo.handle, state={"topic": "x"}, prose="let me in")

    assert caught.value.status == 403


def test_writing_to_a_handle_that_does_not_exist_is_a_404(gabo):
    """404 and 403 are distinguishable on purpose: the agent decides
    differently when a person is unreachable versus has not accepted them.
    """
    with pytest.raises(ProtocolError) as caught:
        gabo.send(to="ghost@doorslip.test", state={}, prose="hello")

    assert caught.value.status == 404


def test_a_message_id_cannot_be_replayed(gabo, tomas, http):
    _introduce(gabo, tomas)
    raw = build(
        sender_handle=gabo.handle,
        sender_agent="hermes",
        sender_pubkey=gabo.pubkey,
        to=tomas.handle,
        state={"topic": "barbecue"},
        prose="Saturday?",
    )
    sealed = seal(raw, gabo._keypair.private_key)
    headers = {SIGNATURE_HEADER: sealed.signature}

    http.post("/inbox", content=sealed.raw, headers=headers)
    replayed = http.post("/inbox", content=sealed.raw, headers=headers)

    assert replayed.status_code == 409


def test_a_parent_from_another_thread_is_refused(gabo, tomas):
    _introduce(gabo, tomas)
    first = gabo.send(to=tomas.handle, state={"topic": "barbecue"}, prose="one")
    other = gabo.send(to=tomas.handle, state={"topic": "something else"}, prose="two")

    with pytest.raises(ProtocolError) as caught:
        gabo.send(
            to=tomas.handle,
            state={"status": "confirmed"},
            prose="three",
            thread_id=other["thread_id"],
            parent_message_id=first["message_id"],
        )

    assert caught.value.status == 400


def test_a_parent_that_never_arrived_is_refused(gabo, tomas):
    _introduce(gabo, tomas)
    started = gabo.send(to=tomas.handle, state={"topic": "barbecue"}, prose="one")

    with pytest.raises(ProtocolError) as caught:
        gabo.send(
            to=tomas.handle,
            state={},
            prose="two",
            thread_id=started["thread_id"],
            parent_message_id="never-existed",
        )

    assert caught.value.status == 400


def test_acknowledging_marks_the_message_as_incorporated(gabo, tomas):
    """Spec §7.7: without this, a broken thread cannot be told apart from a
    broken transport.
    """
    _introduce(gabo, tomas)
    gabo.send(to=tomas.handle, state={"topic": "barbecue"}, prose="Saturday?")
    message_id = tomas.inbox()[0]["message_id"]

    tomas.ack(message_id)

    assert tomas.inbox()[0]["acked"]
    assert tomas.inbox(unacked_only=True) == []


def test_you_cannot_acknowledge_someone_elses_message(gabo, tomas):
    _introduce(gabo, tomas)
    gabo.send(to=tomas.handle, state={"topic": "barbecue"}, prose="Saturday?")
    message_id = tomas.inbox()[0]["message_id"]

    with pytest.raises(ProtocolError) as caught:
        gabo.ack(message_id)

    assert caught.value.status == 404


def test_a_revoked_key_can_no_longer_send(gabo, tomas, http):
    _introduce(gabo, tomas)
    http.post(
        "/revoke-key",
        json={"pubkey": gabo.pubkey},
        headers=gabo._auth_headers(),
    )

    with pytest.raises(ProtocolError) as caught:
        gabo.send(to=tomas.handle, state={}, prose="still here?")

    assert caught.value.status == 401


def test_revocation_does_not_erase_messages_already_received(gabo, tomas, http):
    """Spec §7.6. Retroactive revocation would break every historical thread."""
    _introduce(gabo, tomas)
    gabo.send(to=tomas.handle, state={"topic": "barbecue"}, prose="Saturday?")

    http.post("/revoke-key", json={"pubkey": gabo.pubkey}, headers=gabo._auth_headers())

    assert len(tomas.inbox()) == 1


# -- the welcome desk -----------------------------------------------------


def test_the_welcome_desk_accepts_a_stranger_and_replies(gabo):
    """Spec §8: the one exception to the address-book rule."""
    gabo.send(to=WELCOME, state={"topic": "hello"}, prose="just installed")

    reply = [m for m in gabo.inbox() if m["from"] == WELCOME]
    assert len(reply) == 1
    assert reply[0]["envelope"]["state"]["topic"] == "welcome to Doorslip"


def test_the_welcome_reply_is_identical_every_time(gabo, tomas):
    """It is a template and spends no inference, which is what makes the only
    open endpoint in v0 free to operate and closes the spam vector.
    """
    gabo.send(to=WELCOME, state={"topic": "hello"}, prose="hi")
    tomas.send(to=WELCOME, state={"topic": "hello"}, prose="hi")

    to_gabo = next(m for m in gabo.inbox() if m["from"] == WELCOME)
    to_tomas = next(m for m in tomas.inbox() if m["from"] == WELCOME)

    assert to_gabo["envelope"]["prose"] == to_tomas["envelope"]["prose"]


def test_the_welcome_reply_hangs_off_the_message_it_answers(gabo):
    sent = gabo.send(to=WELCOME, state={"topic": "hello"}, prose="hi")

    reply = next(m for m in gabo.inbox() if m["from"] == WELCOME)

    assert reply["thread_id"] == sent["thread_id"]
    assert reply["parent_message_id"] == sent["message_id"]


# -- authentication -------------------------------------------------------


def test_a_nonce_cannot_be_used_twice_for_authentication(gabo, http):
    from doorslip.auth import AUTH_HEADER, build_credential

    nonce = http.get("/nonce", params={"pubkey": gabo.pubkey}).json()["nonce"]
    header = {AUTH_HEADER: build_credential(gabo.pubkey, nonce, gabo._keypair.private_key)}

    assert http.post("/invite", headers=header).status_code == 201
    assert http.post("/invite", headers=header).status_code == 401


def test_an_unregistered_key_cannot_authenticate(http):
    from doorslip.auth import AUTH_HEADER, build_credential

    stranger = generate_keypair()
    nonce = http.get("/nonce", params={"pubkey": stranger.public_key}).json()["nonce"]

    response = http.post(
        "/invite",
        headers={
            AUTH_HEADER: build_credential(stranger.public_key, nonce, stranger.private_key)
        },
    )

    assert response.status_code == 401


def test_a_credential_signed_by_the_wrong_key_is_refused(gabo, http):
    from doorslip.auth import AUTH_HEADER, build_credential

    impostor = generate_keypair()
    nonce = http.get("/nonce", params={"pubkey": gabo.pubkey}).json()["nonce"]

    response = http.post(
        "/invite",
        headers={AUTH_HEADER: build_credential(gabo.pubkey, nonce, impostor.private_key)},
    )

    assert response.status_code == 401


def test_metrics_count_turns_by_speaker_change_not_by_message(gabo, tomas, http):
    """Eight messages from one person are not eight turns, and the
    done-criterion would otherwise be satisfiable by an agent talking alone.
    """
    _introduce(gabo, tomas)
    started = gabo.send(to=tomas.handle, state={"topic": "barbecue"}, prose="one")
    gabo.send(
        to=tomas.handle,
        state={"where": "my place"},
        prose="two",
        thread_id=started["thread_id"],
        parent_message_id=started["message_id"],
    )

    threads = http.get("/metrics").json()

    assert threads["messages_total"] == 2
    assert threads["average_turns_per_thread"] == 1.0
