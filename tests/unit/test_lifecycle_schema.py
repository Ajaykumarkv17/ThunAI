"""Unit tests for `schemas/lifecycle.py` — the Request_Lifecycle transition
table and validator (Req 15.7, Req 13.9; design.md §4.3)."""

import pytest

from schemas.lifecycle import (
    REQUEST_TRANSITIONS,
    InvalidTransitionError,
    RequestState,
    validate_transition,
)

ALL_STATES = {state.value for state in RequestState}


def test_request_transitions_covers_every_declared_state_as_a_key():
    """Every declared state must be a key in REQUEST_TRANSITIONS, even
    terminal states with an empty transition set."""
    assert set(REQUEST_TRANSITIONS.keys()) == ALL_STATES


def test_resolved_state_is_terminal():
    assert REQUEST_TRANSITIONS[RequestState.RESOLVED] == set()


@pytest.mark.parametrize(
    "current,requested",
    [
        (RequestState.INTAKE_PENDING, RequestState.DISPATCH_ELIGIBLE),
        (RequestState.INTAKE_PENDING, RequestState.NON_DISPATCH_ELIGIBLE),
        (RequestState.NON_DISPATCH_ELIGIBLE, RequestState.DISPATCH_ELIGIBLE),
        (RequestState.NON_DISPATCH_ELIGIBLE, RequestState.MANUAL_TRIAGE),
        (RequestState.MANUAL_TRIAGE, RequestState.DISPATCH_ELIGIBLE),
        (RequestState.DISPATCH_ELIGIBLE, RequestState.ASSIGNED),
        (RequestState.ASSIGNED, RequestState.DISPATCH_ELIGIBLE),
        (RequestState.ASSIGNED, RequestState.EN_ROUTE),
        (RequestState.EN_ROUTE, RequestState.ON_SCENE),
        (RequestState.ON_SCENE, RequestState.VERIFICATION),
        (RequestState.VERIFICATION, RequestState.RESOLVED),
        (RequestState.VERIFICATION, RequestState.DISPATCH_ELIGIBLE),
    ],
)
def test_every_legal_transition_succeeds(current, requested):
    """Every transition declared in REQUEST_TRANSITIONS must validate
    without raising."""
    validate_transition(current, requested)


@pytest.mark.parametrize(
    "current,requested",
    [
        (RequestState.INTAKE_PENDING, RequestState.ASSIGNED),
        (RequestState.INTAKE_PENDING, RequestState.RESOLVED),
        (RequestState.DISPATCH_ELIGIBLE, RequestState.RESOLVED),
        (RequestState.DISPATCH_ELIGIBLE, RequestState.EN_ROUTE),
        (RequestState.EN_ROUTE, RequestState.ASSIGNED),
        (RequestState.RESOLVED, RequestState.DISPATCH_ELIGIBLE),
        (RequestState.RESOLVED, RequestState.INTAKE_PENDING),
        (RequestState.ON_SCENE, RequestState.RESOLVED),
    ],
)
def test_illegal_transitions_raise_invalid_transition_error(current, requested):
    with pytest.raises(InvalidTransitionError) as excinfo:
        validate_transition(current, requested)
    assert excinfo.value.current == current
    assert excinfo.value.requested == requested


def test_invalid_transition_error_names_current_and_requested_state():
    """Req 15.7: the raised error must identify the current state and the
    requested state."""
    with pytest.raises(InvalidTransitionError) as excinfo:
        validate_transition(RequestState.RESOLVED, RequestState.ASSIGNED)
    message = str(excinfo.value)
    assert RequestState.RESOLVED.value in message
    assert RequestState.ASSIGNED.value in message


def test_unknown_current_state_raises_invalid_transition_error():
    """A current state that is not itself a declared key must also be
    rejected rather than raising a KeyError."""
    with pytest.raises(InvalidTransitionError):
        validate_transition("NOT_A_REAL_STATE", RequestState.INTAKE_PENDING)
