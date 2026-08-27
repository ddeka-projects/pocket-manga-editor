"""Shared activity and operation errors for the always-on web application."""

from __future__ import annotations

from enum import Enum


class CompanionActivity(str, Enum):
    """The independent metadata domain used by a reader session."""

    READ = "read"
    EDIT = "edit"


class CoordinatorError(RuntimeError):
    """Base error for web-application coordination failures."""

    code = "coordinator_error"


class OperationBusyError(CoordinatorError):
    code = "operation_busy"


class RescanError(CoordinatorError):
    code = "rescan_failed"


class WrongActivityError(CoordinatorError):
    code = "wrong_activity"
