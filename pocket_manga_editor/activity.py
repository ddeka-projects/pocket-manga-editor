"""Shared activity errors for the always-on web application."""

from __future__ import annotations

from enum import Enum


class Activity(str, Enum):
    """The independent metadata domain used by a reader session."""

    READ = "read"
    EDIT = "edit"


class ServiceError(RuntimeError):
    """Base error for web-application coordination failures."""

    code = "service_error"


class RescanError(ServiceError):
    code = "rescan_failed"


class WrongActivityError(ServiceError):
    code = "wrong_activity"
