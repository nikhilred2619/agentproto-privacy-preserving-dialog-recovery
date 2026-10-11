"""Dialog lifecycle states and allowed transitions.

ESTABLISHING -> ACTIVE <-> INTERRUPTED
ACTIVE/INTERRUPTED -> RESUMING (gateway-side claim with a lease)
RESUMING -> ACTIVE (finalize) | INTERRUPTED (downstream failure)
any non-terminal -> TERMINATED (absorbing)
"""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    ESTABLISHING = "ESTABLISHING"
    ACTIVE = "ACTIVE"
    INTERRUPTED = "INTERRUPTED"
    RESUMING = "RESUMING"
    TERMINATED = "TERMINATED"


ALLOWED: dict[State, set[State]] = {
    State.ESTABLISHING: {State.ACTIVE, State.TERMINATED},
    State.ACTIVE: {State.ACTIVE, State.INTERRUPTED, State.RESUMING, State.TERMINATED},
    State.INTERRUPTED: {State.ACTIVE, State.INTERRUPTED, State.RESUMING, State.TERMINATED},
    State.RESUMING: {State.ACTIVE, State.INTERRUPTED, State.RESUMING, State.TERMINATED},
    State.TERMINATED: set(),
}


class IllegalTransition(Exception):
    pass


def check_transition(old: State | str, new: State | str) -> None:
    o, n = State(old), State(new)
    if n not in ALLOWED[o]:
        raise IllegalTransition(f"{o.value} -> {n.value}")
