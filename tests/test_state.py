"""Thread state reconstruction (spec §6.1)."""

import pytest

from doorslip.state import ThreadBroken, merge_patch, reconstruct


def _message(message_id, parent, state):
    return {"message_id": message_id, "parent_message_id": parent, "state": state}


# -- merge patch ----------------------------------------------------------


def test_a_patch_only_touches_the_keys_it_names():
    result = merge_patch({"topic": "barbecue", "where": "my place"}, {"where": "the park"})

    assert result == {"topic": "barbecue", "where": "the park"}


def test_nested_objects_merge_rather_than_replace():
    result = merge_patch(
        {"budget": {"amount": 100, "currency": "ARS"}}, {"budget": {"amount": 200}}
    )

    assert result == {"budget": {"amount": 200, "currency": "ARS"}}


def test_arrays_are_replaced_whole_not_merged():
    """RFC 7386. To change one task you resend the entire `tasks` array.

    Written as a test because two implementations left to guess will guess
    differently, and the divergence only surfaces once they talk to each other.
    """
    result = merge_patch({"tasks": ["fire", "salads"]}, {"tasks": ["wine"]})

    assert result == {"tasks": ["wine"]}


def test_null_deletes_the_key():
    """The trap: an agent meaning "I do not know yet" must send a value.

    Sending null removes the field from the thread entirely, and the other
    side cannot tell the difference between "unknown" and "withdrawn".
    """
    result = merge_patch({"budget": {"amount": 100}, "where": "my place"}, {"budget": None})

    assert result == {"where": "my place"}


# -- reconstruction -------------------------------------------------------


def test_patches_apply_in_parent_order_not_arrival_order():
    """The whole reason parent_message_id exists.

    These are handed over in reverse. If reconstruction followed arrival or
    timestamp order the result would be wrong, and the done-criterion of §2
    depends on it being the same every time.
    """
    messages = [
        _message("c", "b", {"status": "confirmed"}),
        _message("a", None, {"topic": "barbecue", "status": "proposed"}),
        _message("b", "a", {"where": "my place"}),
    ]

    result = reconstruct(messages)

    assert result.state == {
        "topic": "barbecue",
        "status": "confirmed",
        "where": "my place",
    }
    assert result.applied == ["a", "b", "c"]


def test_a_single_message_thread_reconstructs_to_its_own_state():
    result = reconstruct([_message("a", None, {"topic": "barbecue"})])

    assert result.state == {"topic": "barbecue"}
    assert not result.diverged


def test_two_messages_claiming_the_same_parent_are_reported_as_divergence():
    """Both sides wrote at once. The agent finds out instead of one silently
    overwriting the other. Resolving it is out of scope for v0 by design.
    """
    messages = [
        _message("a", None, {"topic": "barbecue"}),
        _message("b", "a", {"where": "my place"}),
        _message("c", "a", {"where": "the park"}),
    ]

    result = reconstruct(messages)

    assert result.diverged
    assert result.divergences[0].parent_id == "a"
    assert result.divergences[0].message_ids == ["b", "c"]


def test_a_missing_parent_breaks_the_thread_loudly():
    messages = [
        _message("a", None, {"topic": "barbecue"}),
        _message("c", "b-never-arrived", {"status": "confirmed"}),
    ]

    with pytest.raises(ThreadBroken):
        reconstruct(messages)


def test_a_thread_needs_exactly_one_root():
    messages = [
        _message("a", None, {"topic": "barbecue"}),
        _message("b", None, {"topic": "something else"}),
    ]

    with pytest.raises(ThreadBroken):
        reconstruct(messages)


def test_a_cycle_is_refused_instead_of_looping_forever():
    messages = [
        _message("a", None, {"topic": "barbecue"}),
        _message("b", "c", {}),
        _message("c", "b", {}),
    ]

    with pytest.raises(ThreadBroken):
        reconstruct(messages)


def test_an_empty_thread_reconstructs_to_an_empty_state():
    assert reconstruct([]).state == {}


# -- which branch the conversation actually took --------------------------


def test_divergence_follows_the_branch_that_continued():
    """From a real thread. Two agents wrote at once; one message was never
    answered while the other carried the rest of the exchange and the agreed
    ending. Picking the lowest message_id reported the dead stub as the state
    and the actual conclusion as a discarded branch — a thread that had closed
    as `done` read back as still in progress.
    """
    messages = [
        _message("a", None, {"topic": "landing", "status": "proposed"}),
        _message("b-dead-end", "a", {"status": "autonomous"}),
        _message("c-continued", "a", {"status": "proposing-consensus"}),
        _message("d", "c-continued", {"status": "done"}),
    ]

    result = reconstruct(messages)

    assert result.state["status"] == "done"
    assert result.applied == ["a", "c-continued", "d"]
    assert result.diverged


def test_the_divergence_is_still_reported_not_resolved():
    """Depth says where the conversation went. It does not claim to settle who
    was right, and the caller is still told the two sides disagreed.
    """
    messages = [
        _message("a", None, {"topic": "x"}),
        _message("b", "a", {"where": "park"}),
        _message("c", "a", {"where": "my place"}),
        _message("d", "c", {"status": "confirmed"}),
    ]

    result = reconstruct(messages)

    assert result.divergences[0].message_ids == ["b", "c"]
    assert result.state["where"] == "my place"


def test_equal_branches_break_the_tie_deterministically():
    """Two branches of the same length must still reconstruct identically on
    both sides, or the state error the criterion counts becomes unmeasurable.
    """
    messages = [
        _message("a", None, {"topic": "x"}),
        _message("z-branch", "a", {"where": "park"}),
        _message("m-branch", "a", {"where": "my place"}),
    ]

    first = reconstruct(messages)
    shuffled = reconstruct(list(reversed(messages)))

    assert first.state == shuffled.state
    assert first.applied == shuffled.applied


def test_a_long_thread_does_not_exhaust_the_stack():
    """Depth is computed bottom-up rather than by recursing."""
    messages = [_message("m0", None, {"topic": "x"})]
    messages += [_message(f"m{n}", f"m{n - 1}", {"n": n}) for n in range(1, 2000)]

    result = reconstruct(messages)

    assert len(result.applied) == 2000
    assert result.state["n"] == 1999


# -- a divergence somebody settled ----------------------------------------


def _resolving(message_id, parent, state, resolves):
    message = _message(message_id, parent, state)
    message["resolves"] = resolves
    return message


def test_a_settled_divergence_stops_being_reported():
    """A warning that outlives the disagreement teaches people to scroll past
    it, and then the real one goes unread too.
    """
    messages = [
        _message("a", None, {"topic": "landing"}),
        _message("b", "a", {"where": "park"}),
        _message("c", "a", {"where": "my place"}),
        _resolving("d", "c", {"status": "done"}, ["b"]),
    ]

    result = reconstruct(messages)

    assert not result.diverged
    assert result.open_divergences == []
    assert result.divergences[0].resolved_by == "d"


def test_the_record_of_the_disagreement_stays():
    """Quieting the alarm is not erasing the history. Somebody reading the
    thread later should still see the two sides existed.
    """
    messages = [
        _message("a", None, {"topic": "x"}),
        _message("b", "a", {"where": "park"}),
        _message("c", "a", {"where": "my place"}),
        _resolving("d", "c", {}, ["b"]),
    ]

    result = reconstruct(messages)

    assert result.divergences[0].message_ids == ["b", "c"]
    assert result.divergences[0].resolved


def test_an_unsettled_divergence_is_still_reported():
    messages = [
        _message("a", None, {"topic": "x"}),
        _message("b", "a", {"where": "park"}),
        _message("c", "a", {"where": "my place"}),
    ]

    assert reconstruct(messages).diverged


def test_settling_one_divergence_does_not_quiet_another():
    messages = [
        _message("a", None, {"topic": "x"}),
        _message("b", "a", {"n": 1}),
        _message("c", "a", {"n": 2}),
        _resolving("d", "c", {}, ["b"]),
        _message("e", "d", {"m": 1}),
        _message("f", "d", {"m": 2}),
    ]

    result = reconstruct(messages)

    assert result.diverged
    assert len(result.open_divergences) == 1
    assert result.open_divergences[0].parent_id == "d"
