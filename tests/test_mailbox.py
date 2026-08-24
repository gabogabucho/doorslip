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


def test_the_agent_that_started_a_thread_can_still_reconstruct_it(gabo, tomas):
    """Regression: the server files each message into the RECIPIENT's inbox only.

    An agent reading its inbox alone never sees what it wrote itself — so the
    one who opened a thread would be missing the root and reconstruction would
    fail outright. Every agent keeps its own outbox for exactly this reason.
    """
    _introduce(gabo, tomas)
    opened = gabo.send(to=tomas.handle, state={"topic": "barbecue"}, prose="Saturday?")
    tomas.send(
        to=gabo.handle,
        state={"status": "confirmed"},
        prose="Yes",
        thread_id=opened["thread_id"],
        parent_message_id=opened["message_id"],
    )

    from_opener = gabo.thread_state(opened["thread_id"])
    from_replier = tomas.thread_state(opened["thread_id"])

    assert from_opener.state == {"topic": "barbecue", "status": "confirmed"}
    assert from_opener.state == from_replier.state


def test_a_thread_reconstructs_the_same_way_for_both_sides(gabo, tomas):
    """Determinism is what makes "state errors" countable (spec §2)."""
    _introduce(gabo, tomas)
    opened = gabo.send(to=tomas.handle, state={"topic": "barbecue", "where": "park"}, prose="1")
    second = tomas.send(
        to=gabo.handle, state={"where": "my place"}, prose="2",
        thread_id=opened["thread_id"], parent_message_id=opened["message_id"],
    )
    gabo.send(
        to=tomas.handle, state={"status": "confirmed"}, prose="3",
        thread_id=opened["thread_id"], parent_message_id=second["message_id"],
    )

    assert gabo.thread_state(opened["thread_id"]).state == {
        "topic": "barbecue",
        "where": "my place",
        "status": "confirmed",
    }
    assert (
        gabo.thread_state(opened["thread_id"]).state
        == tomas.thread_state(opened["thread_id"]).state
    )


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


def test_the_welcome_reply_is_a_template_and_spends_no_inference(gabo, tomas):
    """What makes the only open endpoint in v0 free to run, and closes the
    only spam vector: the reply is generated, not reasoned about.

    The property is determinism, not byte-equality between people — the text
    carries the recipient's own address, which is the one thing a newcomer
    needs and cannot get anywhere else. Substituting a handle is templating;
    it is thinking that would cost money.
    """
    gabo.send(to=WELCOME, state={"topic": "hello"}, prose="hi")
    gabo.send(to=WELCOME, state={"topic": "hello"}, prose="hi again")
    tomas.send(to=WELCOME, state={"topic": "hello"}, prose="hi")

    to_gabo = [m["envelope"]["prose"] for m in gabo.inbox() if m["from"] == WELCOME]
    to_tomas = next(
        m["envelope"]["prose"] for m in tomas.inbox() if m["from"] == WELCOME
    )

    # Same person, twice: identical down to the byte.
    assert to_gabo[0] == to_gabo[1]
    # Different person: differs only where the address appears.
    assert to_gabo[0] != to_tomas
    assert gabo.handle in to_gabo[0] and tomas.handle in to_tomas
    assert to_gabo[0].replace(gabo.handle, "") == to_tomas.replace(tomas.handle, "")


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

    Counted per thread rather than over the whole database: the server sends
    slips of its own — the welcome reply, the acceptance notice — and a test
    that totals everything breaks whenever one is added, without the thing it
    measures having changed at all.
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

    thread = [m for m in tomas.inbox() if m["thread_id"] == started["thread_id"]]
    senders = {m["from"] for m in thread}

    assert len(thread) == 2
    assert senders == {gabo.handle}  # two messages, one speaker
    assert http.get("/metrics").json()["average_turns_per_thread"] < 2


# -- enrolling a second agent for the same person (spec §7.3) -------------


def _enrol(http, mailbox_owner, label):
    """Attach a brand-new key to an existing mailbox, as a second agent would."""
    code = mailbox_owner.enroll_code()
    joiner = Agent(http, handle="", label=label, keypair=generate_keypair())
    result = joiner.register(enroll_code=code)
    joiner.handle = result["handle"]
    return joiner, result


def test_a_second_agent_joins_the_same_mailbox(gabo, tomas, http):
    """Five agents, one inbox, one address book — the mailbox is the human's."""
    _introduce(gabo, tomas)
    gabo.send(to=tomas.handle, state={"topic": "barbecue"}, prose="Saturday?")

    claude, result = _enrol(http, tomas, "claude")

    assert result["handle"] == tomas.handle
    assert sorted(result["active_agents"]) == ["claude", "claude"]
    assert gabo.handle in claude.contacts()
    assert any(m["from"] == gabo.handle for m in claude.inbox())


def test_enrolling_notifies_the_mailbox_and_the_notice_is_not_signed_by_the_new_key(
    gabo, http
):
    """Spec §7.3. A compromised agent that could sign its own announcement
    would control the warning too, so the server signs it instead.
    """
    claude, _ = _enrol(http, gabo, "claude")

    notice = [m for m in gabo.inbox() if m["from"] == WELCOME]
    assert len(notice) == 1
    assert notice[0]["envelope"]["from"]["pubkey"] != claude.pubkey
    assert "claude" in notice[0]["envelope"]["prose"]


def test_an_enrolment_code_is_single_use(gabo, http):
    code = gabo.enroll_code()
    first = Agent(http, handle="", label="claude", keypair=generate_keypair())
    first.register(enroll_code=code)

    second = Agent(http, handle="", label="codex", keypair=generate_keypair())
    with pytest.raises(ProtocolError) as caught:
        second.register(enroll_code=code)

    assert caught.value.status == 400


def test_an_invitation_code_is_refused_by_register(gabo, http):
    """The mistake this prefix split exists to catch.

    Redeeming an invitation here would enrol another human as your own agent,
    handing them your inbox and the ability to sign as you.
    """
    joiner = Agent(http, handle="x@doorslip.test", label="claude", keypair=generate_keypair())

    with pytest.raises(ProtocolError) as caught:
        joiner.register(enroll_code=gabo.invite())

    assert caught.value.status == 400


def test_a_mailbox_stops_at_five_active_agents(gabo, http):
    """Hardcoded in spec §4. Not a setting: a limit you can raise by editing a
    config file is not a limit.
    """
    for label in ("two", "three", "four", "five"):
        _enrol(http, gabo, label)

    with pytest.raises(ProtocolError) as caught:
        gabo.enroll_code()

    assert caught.value.status == 409


def test_a_revoked_agent_frees_a_slot(gabo, http):
    for label in ("two", "three", "four", "five"):
        _enrol(http, gabo, label)
    extra = [m for m in gabo.inbox()]
    assert extra  # notices arrived

    http.post("/revoke-key", json={"pubkey": gabo.pubkey}, headers=gabo._auth_headers())

    # Gabo's own key is gone, but the mailbox now has a free slot again.
    assert http.get("/nonce", params={"pubkey": gabo.pubkey}).status_code == 200


# -- the inviter hears back (spec §4, symmetry of knowledge) --------------


def test_the_inviter_is_told_when_their_code_is_redeemed(gabo, tomas):
    """The address book becomes symmetric the instant a code is accepted, but
    the knowledge was not: the acceptor is told who they added, the inviter
    learned nothing. In a protocol that makes the connection mutual on
    purpose, leaving one side guessing is an oversight.
    """
    _introduce(gabo, tomas)

    notice = [
        m for m in gabo.inbox()
        if m["from"] == WELCOME
        and m["envelope"]["state"]["topic"] == "an invitation you sent was accepted"
    ]

    assert len(notice) == 1
    assert tomas.handle in notice[0]["envelope"]["prose"]
    assert tomas.handle in notice[0]["envelope"]["state"]["who"]


def test_only_the_inviter_is_told_not_the_acceptor(gabo, tomas):
    """The acceptor already learned it from the reply to their own request."""
    _introduce(gabo, tomas)

    for message in tomas.inbox():
        assert message["envelope"]["state"].get("topic") != "an invitation you sent was accepted"


def test_the_acceptance_notice_is_signed_by_the_server(gabo, tomas):
    """Same rule as the enrolment notice: an announcement the party who caused
    the change could sign is an announcement they could forge.
    """
    _introduce(gabo, tomas)

    notice = next(
        m for m in gabo.inbox()
        if m["envelope"]["state"].get("topic") == "an invitation you sent was accepted"
    )

    assert notice["envelope"]["from"]["pubkey"] != tomas.pubkey
    assert notice["envelope"]["from"]["handle"] == WELCOME


def test_a_refused_code_notifies_nobody(gabo, tomas, http):
    """Nothing happened, so nothing is announced."""
    before = len(gabo.inbox())

    with pytest.raises(ProtocolError):
        tomas.accept("ds_inv_never-issued")

    assert len(gabo.inbox()) == before


# -- reading a thread back ------------------------------------------------


def test_a_thread_reads_as_a_conversation_from_either_side(gabo, tomas):
    """After an exchange nobody supervised, the first thing a human wants is
    to read what was actually said. Neither inbox alone can show it: each
    holds half.
    """
    _introduce(gabo, tomas)
    opened = gabo.send(to=tomas.handle, state={"topic": "barbecue"}, prose="Saturday?")
    tomas.send(
        to=gabo.handle, state={"status": "confirmed"}, prose="Yes",
        thread_id=opened["thread_id"], parent_message_id=opened["message_id"],
    )

    from_gabo = gabo.thread_messages(opened["thread_id"])
    from_tomas = tomas.thread_messages(opened["thread_id"])

    assert [m["prose"] for m in from_gabo] == ["Saturday?", "Yes"]
    assert [m["prose"] for m in from_tomas] == ["Saturday?", "Yes"]
    assert [m["direction"] for m in from_gabo] == ["out", "in"]


def test_a_discarded_branch_is_marked_not_hidden(gabo, tomas):
    """When both sides wrote at once, the branch reconstruction did not follow
    is often the most interesting thing on the page — it is where they were
    about to disagree.
    """
    _introduce(gabo, tomas)
    opened = gabo.send(to=tomas.handle, state={"topic": "barbecue"}, prose="Saturday?")
    for prose in ("my place", "the park"):
        tomas.send(
            to=gabo.handle, state={"where": prose}, prose=prose,
            thread_id=opened["thread_id"], parent_message_id=opened["message_id"],
        )

    messages = gabo.thread_messages(opened["thread_id"])

    assert len(messages) == 3
    assert sum(1 for m in messages if not m["on_main_branch"]) == 1


def test_an_empty_thread_reads_as_nothing(gabo):
    assert gabo.thread_messages("no-such-thread") == []


def test_agents_on_one_machine_share_what_was_sent(gabo, tomas, http, tmp_path):
    """Two agents act for the same person and take part in the same threads.

    A per-agent outbox leaves each holding a different half of the same
    conversation, and a thread one agent started cannot be read back by the
    other at all — which is how a reconstruction failure reached a real user.
    """
    from doorslip.crypto import generate_keypair

    shared = tmp_path / "outbox.jsonl"
    hermes = Agent(
        http, handle=gabo.handle, label="hermes",
        keypair=gabo._keypair, outbox_path=shared,
    )
    _introduce(gabo, tomas)
    opened = hermes.send(to=tomas.handle, state={"topic": "barbecue"}, prose="Saturday?")
    tomas.send(
        to=gabo.handle, state={"status": "confirmed"}, prose="Yes",
        thread_id=opened["thread_id"], parent_message_id=opened["message_id"],
    )

    # A second agent properly enrolled: its own key, the same mailbox.
    claude = Agent(
        http, handle="", label="claude",
        keypair=generate_keypair(), outbox_path=shared,
    )
    claude.handle = claude.register(enroll_code=gabo.enroll_code())["handle"]

    assert claude.thread_state(opened["thread_id"]).state["status"] == "confirmed"


def test_an_unreadable_outbox_line_does_not_cost_the_history(gabo, tmp_path):
    """A truncated write during a crash must not make every earlier thread
    unreadable.
    """
    outbox = tmp_path / "outbox.jsonl"
    agent = Agent(
        gabo._http, handle=gabo.handle, label="hermes",
        keypair=gabo._keypair, outbox_path=outbox,
    )
    agent.send(to=f"welcome@doorslip.test", state={"topic": "hello"}, prose="hi")
    with outbox.open("a", encoding="utf-8") as broken:
        broken.write('{"half a line\n')

    assert len(agent.sent()) == 1
