"""Companion ownership states and fail-closed transition rules."""

from __future__ import annotations

from enum import Enum


class CompanionState(str, Enum):
    DESKTOP_ACTIVE = "desktop_active"
    ENTERING_COMPANION = "entering_companion"
    COMPANION_ACTIVE = "companion_active"
    EXITING_COMPANION = "exiting_companion"
    COMPANION_ERROR = "companion_error"


class CompanionStateError(RuntimeError):
    """Base error for ownership or transition violations."""

    code = "invalid_state"


class DesktopMutationBlocked(CompanionStateError):
    code = "desktop_write_blocked"


class MobileAccessError(CompanionStateError):
    code = "inactive_mode"


class ShutdownTransitionError(MobileAccessError):
    code = "shutdown_transition"


_LEGAL_TRANSITIONS = {
    CompanionState.DESKTOP_ACTIVE: frozenset(
        {CompanionState.ENTERING_COMPANION, CompanionState.COMPANION_ERROR}
    ),
    CompanionState.ENTERING_COMPANION: frozenset(
        {
            CompanionState.DESKTOP_ACTIVE,
            CompanionState.COMPANION_ACTIVE,
            CompanionState.COMPANION_ERROR,
        }
    ),
    CompanionState.COMPANION_ACTIVE: frozenset(
        {CompanionState.EXITING_COMPANION, CompanionState.COMPANION_ERROR}
    ),
    CompanionState.EXITING_COMPANION: frozenset(
        {CompanionState.DESKTOP_ACTIVE, CompanionState.COMPANION_ERROR}
    ),
    CompanionState.COMPANION_ERROR: frozenset({CompanionState.DESKTOP_ACTIVE}),
}


def validate_transition(current: CompanionState, target: CompanionState) -> None:
    if target not in _LEGAL_TRANSITIONS[current]:
        raise CompanionStateError(
            f"Cannot transition Companion Mode from {current.value} to {target.value}."
        )
