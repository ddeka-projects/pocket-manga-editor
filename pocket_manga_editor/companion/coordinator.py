"""Thread-safe Companion Mode ownership and activity coordinator."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import threading

from ..models import ScanResult
from .auth import CredentialStore, PairingManager, PairingOffer
from .lease import ControllerLease, LeaseSnapshot
from .review import (
    ActivityContext,
    PositionMutation,
    ReviewSaveError,
    ReviewService,
    SelectionMutation,
)
from .snapshot import ImageSnapshotEntry, LibrarySnapshot
from .state import (
    CompanionActivity,
    CompanionState,
    CompanionStateError,
    DesktopMutationBlocked,
    MobileAccessError,
    ShutdownTransitionError,
    WrongActivityError,
    validate_transition,
)


@dataclass(frozen=True, slots=True)
class MobileContext:
    activity: CompanionActivity
    manga_id: str
    manga_name: str
    folder_id: str
    folder_name: str
    image_id: str
    image_name: str
    selected_count: int | None


@dataclass(frozen=True, slots=True)
class CoordinatorStatus:
    state: CompanionState
    paired: bool
    pairing_open: bool
    pairing_expires_at: float | None
    active_client: bool
    active_client_id: str | None
    lease_expires_at: float | None
    snapshot_id: str | None
    mobile_context: MobileContext | None
    selected_count: int | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class _ActivityBinding:
    client_id: str
    page_instance_id: str | None
    manga_id: str
    activity: CompanionActivity


class CompanionCoordinator:
    """The single ownership gate shared by desktop UI and HTTP requests."""

    def __init__(
        self,
        *,
        pairing_manager: PairingManager | None = None,
        credential_store: CredentialStore | None = None,
        controller_lease: ControllerLease | None = None,
    ) -> None:
        if pairing_manager is not None and credential_store is not None:
            raise ValueError("Pass either pairing_manager or credential_store, not both.")
        self._lock = threading.RLock()
        self._state = CompanionState.DESKTOP_ACTIVE
        self._auth = pairing_manager or PairingManager(store=credential_store)
        self._lease = controller_lease or ControllerLease()
        self._snapshot: LibrarySnapshot | None = None
        self._review: ReviewService | None = None
        self._activity_binding: _ActivityBinding | None = None
        self._context: MobileContext | None = None
        self._last_error: str | None = None
        self._credential_error: str | None = None
        self._recovery_in_progress = False

    def begin_entry(self) -> None:
        with self._lock:
            validate_transition(self._state, CompanionState.ENTERING_COMPANION)
            self._state = CompanionState.ENTERING_COMPANION
            self._last_error = None
            self._recovery_in_progress = False

    def activate(
        self, working_directory: str | Path, scan_result: ScanResult
    ) -> LibrarySnapshot:
        with self._lock:
            if self._state is not CompanionState.ENTERING_COMPANION:
                raise MobileAccessError("Companion entry has not started.")
            try:
                snapshot = LibrarySnapshot.build(working_directory, scan_result)
                review = ReviewService(snapshot, context_callback=self._receive_context)
            except BaseException as exc:
                self._state = CompanionState.COMPANION_ERROR
                self._last_error = str(exc)
                raise
            validate_transition(self._state, CompanionState.COMPANION_ACTIVE)
            self._snapshot = snapshot
            self._review = review
            self._activity_binding = None
            self._context = None
            self._state = CompanionState.COMPANION_ACTIVE
            return snapshot

    def enter_companion(
        self, working_directory: str | Path, scan_result: ScanResult
    ) -> LibrarySnapshot:
        self.begin_entry()
        return self.activate(working_directory, scan_result)

    def abort_entry(self) -> None:
        with self._lock:
            if self._state is not CompanionState.ENTERING_COMPANION:
                raise MobileAccessError("Companion entry is not in progress.")
            validate_transition(self._state, CompanionState.DESKTOP_ACTIVE)
            self._state = CompanionState.DESKTOP_ACTIVE

    def begin_exit(self) -> MobileContext | None:
        with self._lock:
            if self._state is not CompanionState.COMPANION_ACTIVE:
                raise ShutdownTransitionError("Companion Mode is not active.")
            validate_transition(self._state, CompanionState.EXITING_COMPANION)
            self._state = CompanionState.EXITING_COMPANION
            try:
                if self._review is not None:
                    self._review.flush()
                self._lease.disconnect()
                self._activity_binding = None
                return self._context
            except BaseException as exc:
                self._state = CompanionState.COMPANION_ERROR
                self._last_error = str(exc)
                raise

    def finish_exit(self) -> None:
        with self._lock:
            if self._state is not CompanionState.EXITING_COMPANION:
                raise ShutdownTransitionError("Companion exit is not in progress.")
            validate_transition(self._state, CompanionState.DESKTOP_ACTIVE)
            self._snapshot = None
            self._review = None
            self._activity_binding = None
            self._context = None
            self._state = CompanionState.DESKTOP_ACTIVE

    def fail(self, message: str) -> None:
        with self._lock:
            if self._state is not CompanionState.COMPANION_ERROR:
                validate_transition(self._state, CompanionState.COMPANION_ERROR)
            self._state = CompanionState.COMPANION_ERROR
            self._last_error = str(message)
            self._recovery_in_progress = False
            self._lease.disconnect()
            self._activity_binding = None

    def begin_recovery(self) -> MobileContext | None:
        with self._lock:
            if self._state is not CompanionState.COMPANION_ERROR:
                raise MobileAccessError("Companion Mode is not in an error state.")
            self._lease.disconnect()
            self._snapshot = None
            self._review = None
            self._activity_binding = None
            self._recovery_in_progress = True
            return self._context

    def finish_recovery(self) -> None:
        with self._lock:
            if (
                self._state is not CompanionState.COMPANION_ERROR
                or not self._recovery_in_progress
            ):
                raise MobileAccessError("Companion recovery is not in progress.")
            validate_transition(self._state, CompanionState.DESKTOP_ACTIVE)
            self._snapshot = None
            self._review = None
            self._activity_binding = None
            self._context = None
            self._lease.disconnect()
            self._state = CompanionState.DESKTOP_ACTIVE
            self._last_error = None
            self._recovery_in_progress = False

    def recover_to_desktop(self) -> None:
        raise CompanionStateError(
            "Recovery is two-phase: call begin_recovery(), reconcile and reload "
            "desktop state, then call finish_recovery()."
        )

    def require_desktop_mutation(self) -> None:
        with self._lock:
            if self._state is not CompanionState.DESKTOP_ACTIVE:
                raise DesktopMutationBlocked(
                    "Desktop edits are disabled while Companion Mode owns review state."
                )

    def start_pairing(
        self, *, ttl_seconds: float = 300.0, max_attempts: int = 5
    ) -> PairingOffer:
        return self._auth.open_pairing(
            ttl_seconds=ttl_seconds, max_attempts=max_attempts
        )

    def pair(self, code: str) -> str:
        with self._lock:
            credential = self._auth.pair(code)
            self._lease.disconnect()
            self._activity_binding = None
            self._credential_error = None
            return credential

    def forget_device(self) -> None:
        with self._lock:
            self._lease.disconnect()
            self._activity_binding = None
            try:
                self._auth.forget()
            except OSError as exc:
                self._credential_error = str(exc)
                raise
            else:
                self._credential_error = None

    def disconnect_client(self) -> None:
        with self._lock:
            self._lease.disconnect()
            self._activity_binding = None

    def authorize_device(self, credential: str | None) -> None:
        self._auth.authorize(credential)

    def claim_controller(
        self,
        credential: str | None,
        client_id: str,
        page_instance_id: str | None = None,
    ) -> LeaseSnapshot:
        with self._lock:
            self._require_active_locked()
            self._auth.authorize(credential)
            previous = self._lease.snapshot()
            claimed = self._lease.claim(client_id, page_instance_id)
            if (
                previous.instance_id != claimed.instance_id
                or previous.page_instance_id != claimed.page_instance_id
            ):
                self._activity_binding = None
                self._context = None
            return claimed

    def heartbeat_controller(
        self,
        credential: str | None,
        client_id: str,
        page_instance_id: str | None = None,
    ) -> LeaseSnapshot:
        with self._lock:
            self._require_active_locked()
            self._auth.authorize(credential)
            return self._lease.heartbeat(client_id, page_instance_id)

    def release_controller(
        self,
        credential: str | None,
        client_id: str,
        page_instance_id: str | None = None,
    ) -> None:
        with self._lock:
            self._require_active_locked()
            self._auth.authorize(credential)
            self._lease.release(client_id, page_instance_id)
            self._activity_binding = None

    def library(
        self,
        credential: str | None,
        client_id: str,
        page_instance_id: str | None = None,
    ) -> dict[str, object]:
        with self._lock:
            return self._mobile_review_locked(
                credential, client_id, page_instance_id
            ).library_payload()

    def open_manga(
        self,
        credential: str | None,
        client_id: str,
        manga_id: str,
        activity: CompanionActivity,
        page_instance_id: str | None = None,
    ) -> dict[str, object]:
        with self._lock:
            review = self._mobile_review_locked(
                credential, client_id, page_instance_id
            )
            review.snapshot.manga(manga_id)
            self._activity_binding = _ActivityBinding(
                client_id, page_instance_id, manga_id, activity
            )
            return review.manga_payload(manga_id, activity)

    def folder(
        self,
        credential: str | None,
        client_id: str,
        folder_id: str,
        activity: CompanionActivity,
        page_instance_id: str | None = None,
    ) -> dict[str, object]:
        with self._lock:
            review = self._mobile_review_locked(
                credential, client_id, page_instance_id
            )
            self._require_activity_locked(
                client_id, page_instance_id, activity, folder_id=folder_id
            )
            return review.folder_payload(folder_id, activity)

    def set_position(
        self,
        credential: str | None,
        client_id: str,
        activity: CompanionActivity,
        folder_id: str,
        image_id: str,
        page_instance_id: str | None = None,
    ) -> PositionMutation:
        with self._lock:
            review = self._mobile_review_locked(
                credential, client_id, page_instance_id
            )
            self._require_activity_locked(
                client_id, page_instance_id, activity, folder_id=folder_id
            )
            try:
                return review.set_position(activity, folder_id, image_id)
            except ReviewSaveError as exc:
                if activity is CompanionActivity.EDIT:
                    self.fail(str(exc))
                raise

    def set_selection(
        self,
        credential: str | None,
        client_id: str,
        activity: CompanionActivity,
        folder_id: str,
        image_id: str,
        selected: bool,
        page_instance_id: str | None = None,
    ) -> SelectionMutation:
        with self._lock:
            review = self._mobile_review_locked(
                credential, client_id, page_instance_id
            )
            self._require_activity_locked(
                client_id, page_instance_id, activity, folder_id=folder_id
            )
            if activity is not CompanionActivity.EDIT:
                raise WrongActivityError(
                    "Selections are available only in Edit activity."
                )
            try:
                return review.set_selection(
                    activity, folder_id, image_id, selected
                )
            except ReviewSaveError as exc:
                self.fail(str(exc))
                raise

    def image_for_delivery(
        self,
        credential: str | None,
        client_id: str,
        image_id: str,
        page_instance_id: str | None = None,
    ) -> tuple[LibrarySnapshot, ImageSnapshotEntry]:
        with self._lock:
            review = self._mobile_review_locked(
                credential, client_id, page_instance_id
            )
            image = review.snapshot.image(image_id)
            self._require_activity_locked(
                client_id,
                page_instance_id,
                None,
                folder_id=image.folder_id,
            )
            return review.snapshot, image

    def status(self) -> CoordinatorStatus:
        with self._lock:
            pairing = self._auth.pairing_offer
            lease = self._lease.snapshot()
            return CoordinatorStatus(
                self._state,
                self._auth.paired,
                pairing is not None,
                pairing.expires_at if pairing is not None else None,
                lease.connected,
                lease.instance_id,
                lease.lease_expires_at,
                self._snapshot.snapshot_id if self._snapshot is not None else None,
                self._context,
                self._context.selected_count if self._context is not None else None,
                self._last_error or self._credential_error,
            )

    def _mobile_review_locked(
        self,
        credential: str | None,
        client_id: str,
        page_instance_id: str | None = None,
    ) -> ReviewService:
        self._require_active_locked()
        self._auth.authorize(credential)
        self._lease.authorize(client_id, page_instance_id)
        assert self._review is not None
        return self._review

    def _require_activity_locked(
        self,
        client_id: str,
        page_instance_id: str | None,
        activity: CompanionActivity | None,
        *,
        folder_id: str,
    ) -> None:
        binding = self._activity_binding
        if (
            binding is None
            or binding.client_id != client_id
            or binding.page_instance_id != page_instance_id
            or (activity is not None and binding.activity is not activity)
        ):
            raise WrongActivityError(
                "Choose the matching Read or Edit activity before continuing."
            )
        assert self._snapshot is not None
        folder = self._snapshot.folder(folder_id)
        if folder.manga_id != binding.manga_id:
            raise WrongActivityError(
                "This folder is outside the chosen manga activity."
            )

    def _require_active_locked(self) -> None:
        if self._state is CompanionState.EXITING_COMPANION:
            raise ShutdownTransitionError("Companion Mode is shutting down.")
        if self._state is not CompanionState.COMPANION_ACTIVE:
            raise MobileAccessError("Companion Mode is not active.")

    def _receive_context(self, context: ActivityContext) -> None:
        with self._lock:
            self._context = MobileContext(**asdict(context))
