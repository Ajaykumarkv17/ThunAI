"""Request_Lifecycle: the declared set of emergency request states and the
permitted transitions between them (Req 15.7, Req 13.9; design.md §4.3).

This module is deliberately dependency-free (stdlib `enum` + `typing` only) so
it can be imported from anywhere in the codebase — including `infra/` and
`tests/` — without pulling in Pydantic, boto3, or the Strands SDK.
"""

from __future__ import annotations

from enum import Enum


class RequestState(str, Enum):
    """Every declared state an `EmergencyRequest` can hold in State_Store.

    Values match the state names used verbatim in requirements.md and in the
    design.md §4.3 state diagram, so a persisted state string round-trips
    through this enum without translation.
    """

    INTAKE_PENDING = "INTAKE_PENDING"
    NON_DISPATCH_ELIGIBLE = "NON_DISPATCH_ELIGIBLE"
    MANUAL_TRIAGE = "MANUAL_TRIAGE"
    DISPATCH_ELIGIBLE = "DISPATCH_ELIGIBLE"
    ASSIGNED = "ASSIGNED"
    EN_ROUTE = "EN_ROUTE"
    ON_SCENE = "ON_SCENE"
    VERIFICATION = "VERIFICATION"
    RESOLVED = "RESOLVED"


class InvalidTransitionError(Exception):
    """Raised when a requested Request_Lifecycle transition is not declared.

    Carries the current state and the requested state so a caller can surface
    "an error identifying the current state and the requested state" verbatim
    (Req 15.7) without re-deriving them.
    """

    def __init__(self, current: str, requested: str) -> None:
        self.current = current
        self.requested = requested
        super().__init__(
            f"Invalid Request_Lifecycle transition: {current!r} -> {requested!r} "
            "is not a declared transition."
        )


# The declared set of emergency request states and the permitted transitions
# between them (design.md §4.3). Every state is present as a key, including
# RESOLVED, which is terminal and therefore maps to an empty set.
REQUEST_TRANSITIONS: dict[str, set[str]] = {
    RequestState.INTAKE_PENDING: {
        RequestState.DISPATCH_ELIGIBLE,
        RequestState.NON_DISPATCH_ELIGIBLE,
    },
    RequestState.NON_DISPATCH_ELIGIBLE: {
        RequestState.DISPATCH_ELIGIBLE,
        RequestState.MANUAL_TRIAGE,
    },
    # After a coordinator manually categorises a manual-triage request.
    RequestState.MANUAL_TRIAGE: {
        RequestState.DISPATCH_ELIGIBLE,
    },
    RequestState.DISPATCH_ELIGIBLE: {
        RequestState.ASSIGNED,
    },
    RequestState.ASSIGNED: {
        RequestState.DISPATCH_ELIGIBLE,
        RequestState.EN_ROUTE,
    },
    RequestState.EN_ROUTE: {
        RequestState.ON_SCENE,
    },
    RequestState.ON_SCENE: {
        RequestState.VERIFICATION,
    },
    RequestState.VERIFICATION: {
        RequestState.RESOLVED,
        RequestState.DISPATCH_ELIGIBLE,
    },
    RequestState.RESOLVED: set(),  # terminal
}


def validate_transition(current: str, requested: str) -> None:
    """Validate a Request_Lifecycle transition.

    Applies a state transition to a request only along the transitions
    declared in the Request_Lifecycle (Req 15.7, Req 13.9). Succeeds silently
    when the transition is declared; raises otherwise. Never mutates any
    state itself — callers remain responsible for leaving the request in its
    current state on rejection (Req 15.7).

    Args:
        current: The request's current state.
        requested: The state a caller wants to transition the request to.

    Raises:
        InvalidTransitionError: If `requested` is not a declared transition
            target from `current` (including when `current` is not itself a
            declared state).
    """
    if requested not in REQUEST_TRANSITIONS.get(current, set()):
        raise InvalidTransitionError(current=current, requested=requested)
