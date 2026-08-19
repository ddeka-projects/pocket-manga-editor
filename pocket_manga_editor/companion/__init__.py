"""Thread-safe Companion Mode services with no Qt dependencies."""

from .auth import (
    CredentialPersistenceError,
    CredentialVerifierStore,
    PairingManager,
    PairingOffer,
    UnpairedError,
)
from .coordinator import (
    CompanionCoordinator,
    CoordinatorStatus,
    MobileContext,
)
from .server import CompanionHTTPService, HTTPServiceStatus
from .snapshot import LibrarySnapshot
from .state import CompanionActivity, CompanionState

__all__ = [
    "CompanionCoordinator",
    "CompanionHTTPService",
    "CompanionActivity",
    "CompanionState",
    "CoordinatorStatus",
    "CredentialPersistenceError",
    "CredentialVerifierStore",
    "HTTPServiceStatus",
    "LibrarySnapshot",
    "MobileContext",
    "PairingManager",
    "PairingOffer",
    "UnpairedError",
]
