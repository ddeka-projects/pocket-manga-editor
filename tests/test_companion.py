from __future__ import annotations

import hashlib
import http.client
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from pocket_manga_editor.companion.api import (
    APIResponse,
    COOKIE_NAME,
    CompanionAPI,
    StreamingBody,
)
from pocket_manga_editor.companion.auth import (
    AuthenticationError,
    CredentialVerifierStore,
    InvalidPairingCodeError,
    PairingClosedError,
    PairingManager,
    PairingRateLimitedError,
    UnpairedError,
)
from pocket_manga_editor.companion.coordinator import CompanionCoordinator
from pocket_manga_editor.companion.lease import (
    ControllerLease,
    LeaseConflictError,
    LeaseExpiredError,
)
from pocket_manga_editor.companion.review import ReviewService
from pocket_manga_editor.companion.server import CompanionHTTPService
from pocket_manga_editor.companion.snapshot import (
    LibrarySnapshot,
    SnapshotError,
    SnapshotLookupError,
)
from pocket_manga_editor.companion.state import (
    CompanionState,
    CompanionStateError,
    DesktopMutationBlocked,
    MobileAccessError,
    ShutdownTransitionError,
    validate_transition,
)
from pocket_manga_editor.scanner import scan_working_directory
from pocket_manga_editor.storage import SessionStore


class FakeClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class CompanionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.first_page = self.add_page("Series One", "Vol. 01", "001.jpg", b"jpg-one")
        self.second_page = self.add_page("Series One", "Vol. 01", "002.PNG", b"png-two")
        self.other_page = self.add_page("Series Two", "Vol. 02", "001.jpg", b"jpg-three")
        self.scan_result = scan_working_directory(self.root)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def add_page(
        self,
        manga: str,
        volume: str,
        filename: str,
        content: bytes,
    ) -> Path:
        page = self.root / manga / volume / filename
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_bytes(content)
        return page

    def build_snapshot(self, *, token_prefix: str = "opaque") -> LibrarySnapshot:
        counter = iter(range(100))
        return LibrarySnapshot.build(
            self.root,
            self.scan_result,
            id_factory=lambda: f"{token_prefix}{next(counter)}",
        )


class CompanionStateTests(unittest.TestCase):
    def test_only_declared_ownership_transitions_are_legal(self) -> None:
        legal = {
            (CompanionState.DESKTOP_ACTIVE, CompanionState.ENTERING_COMPANION),
            (CompanionState.DESKTOP_ACTIVE, CompanionState.COMPANION_ERROR),
            (CompanionState.ENTERING_COMPANION, CompanionState.DESKTOP_ACTIVE),
            (CompanionState.ENTERING_COMPANION, CompanionState.COMPANION_ACTIVE),
            (CompanionState.ENTERING_COMPANION, CompanionState.COMPANION_ERROR),
            (CompanionState.COMPANION_ACTIVE, CompanionState.EXITING_COMPANION),
            (CompanionState.COMPANION_ACTIVE, CompanionState.COMPANION_ERROR),
            (CompanionState.EXITING_COMPANION, CompanionState.DESKTOP_ACTIVE),
            (CompanionState.EXITING_COMPANION, CompanionState.COMPANION_ERROR),
            (CompanionState.COMPANION_ERROR, CompanionState.DESKTOP_ACTIVE),
        }

        for current in CompanionState:
            for target in CompanionState:
                with self.subTest(current=current, target=target):
                    if (current, target) in legal:
                        validate_transition(current, target)
                    else:
                        with self.assertRaises(CompanionStateError):
                            validate_transition(current, target)


class PairingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.clock = FakeClock()
        self.store = CredentialVerifierStore(self.root / "device.json")
        self.credential = "device-secret-with-sufficient-entropy"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def manager(self) -> PairingManager:
        return PairingManager(
            store=self.store,
            clock=self.clock,
            code_factory=lambda: "123456",
            credential_factory=lambda: self.credential,
        )

    def test_pairing_persists_only_a_verifier_and_survives_restart(self) -> None:
        manager = self.manager()
        with self.assertRaises(UnpairedError):
            manager.authorize(None)
        offer = manager.open_pairing(ttl_seconds=30, max_attempts=3)

        issued_credential = manager.pair(offer.code)

        self.assertEqual(issued_credential, self.credential)
        raw = self.store.path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        self.assertNotIn(self.credential, raw)
        self.assertNotIn(offer.code, raw)
        self.assertEqual(
            payload["credential_verifier"],
            hashlib.sha256(self.credential.encode("utf-8")).hexdigest(),
        )

        restarted = self.manager()
        self.assertTrue(restarted.paired)
        restarted.authorize(self.credential)
        with self.assertRaises(AuthenticationError):
            restarted.authorize("wrong-device-secret")

        restarted.forget()
        self.assertFalse(restarted.paired)
        self.assertFalse(self.store.path.exists())
        with self.assertRaises(AuthenticationError):
            restarted.authorize(self.credential)

    def test_pairing_window_expires_and_failed_attempts_close_it(self) -> None:
        manager = self.manager()
        manager.open_pairing(ttl_seconds=10, max_attempts=2)
        with self.assertRaises(InvalidPairingCodeError):
            manager.pair("000000")
        with self.assertRaises(PairingRateLimitedError):
            manager.pair("000000")
        with self.assertRaises(PairingClosedError):
            manager.pair("123456")

        manager.open_pairing(ttl_seconds=10)
        self.clock.advance(10)
        with self.assertRaises(PairingClosedError):
            manager.pair("123456")

    def test_failed_persistent_revocation_still_invalidates_live_credential(self) -> None:
        manager = self.manager()
        manager.open_pairing()
        manager.pair("123456")

        with patch.object(self.store, "clear", side_effect=OSError("read only")):
            with self.assertRaises(OSError):
                manager.forget()

        self.assertTrue(manager.revocation_pending)
        self.assertTrue(manager.paired)
        with self.assertRaises(AuthenticationError):
            manager.authorize(self.credential)

        manager.forget()
        self.assertFalse(manager.revocation_pending)
        self.assertFalse(manager.paired)


class ControllerLeaseTests(unittest.TestCase):
    def test_one_controller_owns_the_lease_and_can_reclaim_during_grace(self) -> None:
        clock = FakeClock()
        lease = ControllerLease(ttl_seconds=10, grace_seconds=20, clock=clock)

        first = lease.claim("phone-window")
        self.assertTrue(first.connected)
        with self.assertRaises(LeaseConflictError):
            lease.claim("second-tab")

        clock.advance(11)
        with self.assertRaises(LeaseExpiredError):
            lease.authorize("phone-window")
        with self.assertRaises(LeaseConflictError):
            lease.claim("second-tab")

        reclaimed = lease.claim("phone-window")
        self.assertTrue(reclaimed.connected)
        lease.release("phone-window")
        self.assertEqual(lease.claim("second-tab").instance_id, "second-tab")

    def test_new_controller_can_claim_only_after_the_grace_period(self) -> None:
        clock = FakeClock()
        lease = ControllerLease(ttl_seconds=5, grace_seconds=10, clock=clock)
        lease.claim("first")

        clock.advance(16)

        self.assertEqual(lease.claim("second").instance_id, "second")
        with self.assertRaises(LeaseConflictError):
            lease.release("first")

    def test_duplicated_tab_with_same_client_id_cannot_share_live_lease(self) -> None:
        clock = FakeClock()
        lease = ControllerLease(ttl_seconds=5, grace_seconds=10, clock=clock)
        lease.claim("browser-session", "page-one")

        with self.assertRaises(LeaseConflictError):
            lease.claim("browser-session", "page-two")
        with self.assertRaises(LeaseConflictError):
            lease.heartbeat("browser-session", "page-two")

        self.assertTrue(
            lease.authorize("browser-session", "page-one").connected
        )

    def test_same_client_new_page_takes_over_after_ttl_not_grace(self) -> None:
        clock = FakeClock()
        lease = ControllerLease(ttl_seconds=5, grace_seconds=10, clock=clock)
        lease.claim("browser-session", "old-page")

        clock.advance(6)

        replacement = lease.claim("browser-session", "new-page")
        self.assertTrue(replacement.connected)
        self.assertTrue(
            lease.authorize("browser-session", "new-page").connected
        )
        with self.assertRaises(LeaseConflictError):
            lease.authorize("browser-session", "old-page")

    def test_different_client_still_waits_for_full_grace_with_page_nonce(self) -> None:
        clock = FakeClock()
        lease = ControllerLease(ttl_seconds=5, grace_seconds=10, clock=clock)
        lease.claim("first-session", "first-page")

        clock.advance(6)
        with self.assertRaises(LeaseConflictError):
            lease.claim("second-session", "second-page")

        clock.advance(10)
        claimed = lease.claim("second-session", "second-page")
        self.assertEqual(claimed.instance_id, "second-session")


class LibrarySnapshotTests(CompanionFixture):
    def test_snapshot_ids_are_opaque_scoped_and_change_between_sessions(self) -> None:
        first = self.build_snapshot(token_prefix="alpha")
        second = self.build_snapshot(token_prefix="beta")
        first_manga = first.mangas[0]
        first_volume_id = first_manga.volume_ids[0]
        first_page_id = first.volume(first_volume_id).page_ids[0]

        public_ids = {
            first.snapshot_id,
            *(manga.id for manga in first.mangas),
            *(
                volume_id
                for manga in first.mangas
                for volume_id in manga.volume_ids
            ),
            *(
                page_id
                for manga in first.mangas
                for volume_id in manga.volume_ids
                for page_id in first.volume(volume_id).page_ids
            ),
        }
        for public_id in public_ids:
            self.assertNotIn("Series", public_id)
            self.assertNotIn("Vol.", public_id)
            self.assertNotIn("/", public_id)
            self.assertNotIn(str(self.root), public_id)

        self.assertNotEqual(first.snapshot_id, second.snapshot_id)
        with self.assertRaises(SnapshotLookupError):
            second.manga(first_manga.id)
        with self.assertRaises(SnapshotLookupError):
            second.volume(first_volume_id)
        with self.assertRaises(SnapshotLookupError):
            second.page(first_page_id)
        with self.assertRaises(SnapshotLookupError):
            first.page(str(self.first_page))

    def test_page_ids_cannot_be_used_with_a_different_volume(self) -> None:
        snapshot = self.build_snapshot()
        first_volume_id = snapshot.mangas[0].volume_ids[0]
        second_volume_id = snapshot.mangas[1].volume_ids[0]
        first_page_id = snapshot.volume(first_volume_id).page_ids[0]

        self.assertEqual(
            snapshot.page_in_volume(first_volume_id, first_page_id).id,
            first_page_id,
        )
        with self.assertRaises(SnapshotLookupError):
            snapshot.page_in_volume(second_volume_id, first_page_id)

    def test_snapshot_rejects_an_intermediate_symlinked_page_path(self) -> None:
        volume_folder = self.first_page.parent
        relocated = self.root / "relocated-volume"
        volume_folder.rename(relocated)
        try:
            volume_folder.symlink_to(relocated, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory symlinks are unavailable: {exc}")

        with self.assertRaises(SnapshotError):
            LibrarySnapshot.build(self.root, self.scan_result)


class ReviewPersistenceTests(CompanionFixture):
    def test_selection_and_position_are_persisted_before_commands_return(self) -> None:
        snapshot = self.build_snapshot()
        volume_id = snapshot.mangas[0].volume_ids[0]
        volume = snapshot.volume(volume_id)
        first_page_id, second_page_id = volume.page_ids
        review = ReviewService(snapshot)

        selection = review.set_selection(volume_id, first_page_id, True)

        stored = SessionStore(self.root).load(volume.ref)
        self.assertEqual(stored.current_index, 0)
        self.assertEqual(
            stored.selected_paths,
            {snapshot.page(first_page_id).ref.relative_path},
        )
        self.assertEqual(selection.revision, 1)
        self.assertTrue(selection.selected)

        repeated = review.set_selection(volume_id, first_page_id, True)
        self.assertEqual(repeated.revision, selection.revision)

        position = review.set_position(volume_id, second_page_id)

        stored = SessionStore(self.root).load(volume.ref)
        self.assertEqual(stored.current_index, 1)
        self.assertEqual(
            stored.selected_paths,
            {snapshot.page(first_page_id).ref.relative_path},
        )
        self.assertEqual(position.current_page_id, second_page_id)
        self.assertEqual(position.revision, 2)

    def test_payloads_expose_ids_and_labels_but_no_filesystem_paths(self) -> None:
        snapshot = self.build_snapshot()
        volume_id = snapshot.mangas[0].volume_ids[0]
        payload = ReviewService(snapshot).volume_payload(volume_id)
        serialized = json.dumps(payload)

        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("Vol. 01/001.jpg", serialized)
        self.assertEqual(payload["snapshot_id"], snapshot.snapshot_id)
        self.assertTrue(
            all(
                page["image_url"].startswith("/api/page/p_")
                for page in payload["volume"]["pages"]
            )
        )

    def test_selection_of_another_page_does_not_regress_saved_position(self) -> None:
        snapshot = self.build_snapshot()
        volume_id = snapshot.mangas[0].volume_ids[0]
        volume = snapshot.volume(volume_id)
        first_page_id, second_page_id = volume.page_ids
        review = ReviewService(snapshot)

        review.set_position(volume_id, second_page_id)
        mutation = review.set_selection(volume_id, first_page_id, True)

        stored = SessionStore(self.root).load(volume.ref)
        self.assertEqual(stored.current_index, 1)
        self.assertEqual(mutation.current_page_id, second_page_id)
        self.assertEqual(
            stored.selected_paths,
            {snapshot.page(first_page_id).ref.relative_path},
        )


class CoordinatorLifecycleTests(CompanionFixture):
    def setUp(self) -> None:
        super().setUp()
        self.credential = "remembered-device-credential-value"
        pairing = PairingManager(
            code_factory=lambda: "654321",
            credential_factory=lambda: self.credential,
        )
        pairing.open_pairing()
        pairing.pair("654321")
        self.clock = FakeClock()
        self.coordinator = CompanionCoordinator(
            pairing_manager=pairing,
            controller_lease=ControllerLease(clock=self.clock),
        )

    def test_entry_active_exit_transfers_and_restores_write_authority(self) -> None:
        self.coordinator.require_desktop_mutation()

        self.coordinator.begin_entry()

        self.assertEqual(
            self.coordinator.status().state,
            CompanionState.ENTERING_COMPANION,
        )
        with self.assertRaises(DesktopMutationBlocked):
            self.coordinator.require_desktop_mutation()
        with self.assertRaises(MobileAccessError):
            self.coordinator.claim_controller(self.credential, "phone")

        snapshot = self.coordinator.activate(self.root, self.scan_result)
        self.coordinator.claim_controller(self.credential, "phone")
        volume_id = snapshot.mangas[0].volume_ids[0]
        page_id = snapshot.volume(volume_id).page_ids[0]
        self.coordinator.set_selection(
            self.credential, "phone", volume_id, page_id, True
        )

        final_context = self.coordinator.begin_exit()

        self.assertIsNotNone(final_context)
        self.assertEqual(
            self.coordinator.status().state,
            CompanionState.EXITING_COMPANION,
        )
        with self.assertRaises(ShutdownTransitionError):
            self.coordinator.set_position(
                self.credential, "phone", volume_id, page_id
            )
        with self.assertRaises(DesktopMutationBlocked):
            self.coordinator.require_desktop_mutation()

        self.coordinator.finish_exit()

        status = self.coordinator.status()
        self.assertEqual(status.state, CompanionState.DESKTOP_ACTIVE)
        self.assertIsNone(status.snapshot_id)
        self.assertFalse(status.active_client)
        self.coordinator.require_desktop_mutation()
        with self.assertRaises(MobileAccessError):
            self.coordinator.claim_controller(self.credential, "phone")

    def test_error_state_is_fail_closed_until_explicit_recovery(self) -> None:
        self.coordinator.fail("uncertain save")

        status = self.coordinator.status()
        self.assertEqual(status.state, CompanionState.COMPANION_ERROR)
        self.assertEqual(status.last_error, "uncertain save")
        with self.assertRaises(DesktopMutationBlocked):
            self.coordinator.require_desktop_mutation()
        with self.assertRaises(MobileAccessError):
            self.coordinator.claim_controller(self.credential, "phone")

        self.coordinator.begin_recovery()

        with self.assertRaises(DesktopMutationBlocked):
            self.coordinator.require_desktop_mutation()
        with self.assertRaises(MobileAccessError):
            self.coordinator.claim_controller(self.credential, "phone")

        self.coordinator.finish_recovery()

        self.assertEqual(
            self.coordinator.status().state,
            CompanionState.DESKTOP_ACTIVE,
        )
        self.coordinator.require_desktop_mutation()


class CompanionAPITests(CompanionFixture):
    HOST = "desktop.local:8787"
    ORIGIN = "http://desktop.local:8787"
    CLIENT_ID = "phone-home-screen"
    PAGE_INSTANCE_ID = "page-instance-one"

    def setUp(self) -> None:
        super().setUp()
        self.credential = "api-device-credential-with-entropy"
        pairing = PairingManager(
            code_factory=lambda: "112233",
            credential_factory=lambda: self.credential,
        )
        pairing.open_pairing()
        pairing.pair("112233")
        self.clock = FakeClock()
        self.coordinator = CompanionCoordinator(
            pairing_manager=pairing,
            controller_lease=ControllerLease(clock=self.clock),
        )
        self.snapshot = self.coordinator.enter_companion(
            self.root, self.scan_result
        )
        self.api = CompanionAPI(
            self.coordinator,
            allowed_hosts={"desktop.local"},
        )

    @staticmethod
    def payload(response) -> dict[str, object]:
        return json.loads(response.body.decode("utf-8"))

    def headers(
        self,
        *,
        credential: str | None = None,
        client_id: str | None = None,
        page_instance_id: str | None = None,
        content_type: str | None = None,
        origin: str | None = None,
        host: str | None = None,
    ) -> dict[str, str]:
        headers = {"Host": host or self.HOST}
        if credential is not None:
            headers["Cookie"] = f"{COOKIE_NAME}={credential}"
        if client_id is not None:
            headers["X-Companion-Instance"] = client_id
            headers["X-Companion-Page"] = (
                page_instance_id or self.PAGE_INSTANCE_ID
            )
        if content_type is not None:
            headers["Content-Type"] = content_type
        if origin is not None:
            headers["Origin"] = origin
        return headers

    def json_request(
        self,
        method: str,
        target: str,
        payload: dict[str, object],
        *,
        credential: str | None = None,
        client_id: str | None = None,
        page_instance_id: str | None = None,
        origin: str | None = ORIGIN,
        content_type: str = "application/json; charset=utf-8",
        host: str | None = None,
    ):
        return self.api.handle(
            method,
            target,
            self.headers(
                credential=credential,
                client_id=client_id,
                page_instance_id=page_instance_id,
                content_type=content_type,
                origin=origin,
                host=host,
            ),
            json.dumps(payload).encode("utf-8"),
        )

    def claim(
        self,
        client_id: str = CLIENT_ID,
        page_instance_id: str = PAGE_INSTANCE_ID,
    ):
        return self.json_request(
            "POST",
            "/api/controller/claim",
            {"client_id": client_id, "page_id": page_instance_id},
            credential=self.credential,
        )

    def test_status_is_public_minimal_while_library_requires_auth_and_lease(self) -> None:
        status = self.api.handle("GET", "/api/status", {"Host": self.HOST})

        self.assertEqual(status.status, 200)
        status_payload = self.payload(status)
        self.assertTrue(status_payload["status"]["companion_active"])
        serialized = status.body.decode("utf-8")
        self.assertNotIn("Series One", serialized)
        self.assertNotIn(str(self.root), serialized)

        unauthorized = self.api.handle(
            "GET",
            "/api/library",
            self.headers(client_id=self.CLIENT_ID),
        )
        self.assertEqual(unauthorized.status, 401)
        self.assertEqual(
            self.payload(unauthorized)["error"]["code"],
            "unauthorized",
        )

        unclaimed = self.api.handle(
            "GET",
            "/api/library",
            self.headers(
                credential=self.credential,
                client_id=self.CLIENT_ID,
            ),
        )
        self.assertEqual(unclaimed.status, 409)
        self.assertEqual(
            self.payload(unclaimed)["error"]["code"],
            "lease_expired",
        )

    def test_claim_heartbeat_contention_and_release_enforce_one_controller(self) -> None:
        claimed = self.claim()
        self.assertEqual(claimed.status, 200)
        self.assertEqual(
            self.payload(claimed)["snapshot_id"],
            self.snapshot.snapshot_id,
        )

        blocked = self.claim("second-tab")
        self.assertEqual(blocked.status, 423)
        self.assertEqual(
            self.payload(blocked)["error"]["code"],
            "lease_conflict",
        )

        heartbeat = self.json_request(
            "POST",
            "/api/controller/heartbeat",
            {
                "client_id": self.CLIENT_ID,
                "page_id": self.PAGE_INSTANCE_ID,
            },
            credential=self.credential,
        )
        self.assertEqual(heartbeat.status, 200)

        released = self.json_request(
            "POST",
            "/api/controller/release",
            {
                "client_id": self.CLIENT_ID,
                "page_id": self.PAGE_INSTANCE_ID,
            },
            credential=self.credential,
        )
        self.assertEqual(released.status, 200)

        after_release = self.api.handle(
            "GET",
            "/api/library",
            self.headers(
                credential=self.credential,
                client_id=self.CLIENT_ID,
            ),
        )
        self.assertEqual(after_release.status, 409)
        self.assertEqual(
            self.payload(after_release)["error"]["code"],
            "lease_expired",
        )

    def test_duplicated_tab_is_blocked_until_same_session_lease_ttl_expires(
        self,
    ) -> None:
        self.assertEqual(self.claim(page_instance_id="original-page").status, 200)

        duplicate = self.claim(page_instance_id="duplicated-page")
        self.assertEqual(duplicate.status, 423)
        self.assertEqual(
            self.payload(duplicate)["error"]["code"],
            "lease_conflict",
        )

        self.clock.advance(31)
        replacement = self.claim(page_instance_id="reloaded-page")
        self.assertEqual(replacement.status, 200)

        stale_page = self.api.handle(
            "GET",
            "/api/library",
            self.headers(
                credential=self.credential,
                client_id=self.CLIENT_ID,
                page_instance_id="original-page",
            ),
        )
        self.assertEqual(stale_page.status, 423)

        current_page = self.api.handle(
            "GET",
            "/api/library",
            self.headers(
                credential=self.credential,
                client_id=self.CLIENT_ID,
                page_instance_id="reloaded-page",
            ),
        )
        self.assertEqual(current_page.status, 200)

    def test_controller_routes_and_authenticated_routes_require_page_nonce(
        self,
    ) -> None:
        missing_claim_page = self.json_request(
            "POST",
            "/api/controller/claim",
            {"client_id": self.CLIENT_ID},
            credential=self.credential,
        )
        self.assertEqual(missing_claim_page.status, 400)

        self.assertEqual(self.claim().status, 200)
        for route in ("heartbeat", "release"):
            with self.subTest(route=route):
                response = self.json_request(
                    "POST",
                    f"/api/controller/{route}",
                    {"client_id": self.CLIENT_ID},
                    credential=self.credential,
                )
                self.assertEqual(response.status, 400)

        missing_header = self.api.handle(
            "GET",
            "/api/library",
            {
                "Host": self.HOST,
                "Cookie": f"{COOKIE_NAME}={self.credential}",
                "X-Companion-Instance": self.CLIENT_ID,
            },
        )
        self.assertEqual(missing_header.status, 400)

    def test_library_manga_volume_and_write_routes_round_trip_existing_state(self) -> None:
        self.assertEqual(self.claim().status, 200)
        authenticated = self.headers(
            credential=self.credential,
            client_id=self.CLIENT_ID,
        )

        library_response = self.api.handle(
            "GET", "/api/library", authenticated
        )
        self.assertEqual(library_response.status, 200)
        library = self.payload(library_response)
        manga_id = library["mangas"][0]["id"]
        self.assertNotIn(str(self.root), library_response.body.decode("utf-8"))

        manga_response = self.api.handle(
            "GET", f"/api/manga/{manga_id}", authenticated
        )
        self.assertEqual(manga_response.status, 200)
        volume_id = self.payload(manga_response)["manga"]["volumes"][0]["id"]

        volume_response = self.api.handle(
            "GET", f"/api/volume/{volume_id}", authenticated
        )
        self.assertEqual(volume_response.status, 200)
        pages = self.payload(volume_response)["volume"]["pages"]
        first_page_id, second_page_id = (page["id"] for page in pages)

        selected = self.json_request(
            "PUT",
            f"/api/volume/{volume_id}/selection",
            {"page_id": first_page_id, "selected": True},
            credential=self.credential,
            client_id=self.CLIENT_ID,
        )
        self.assertEqual(selected.status, 200)
        self.assertTrue(self.payload(selected)["review"]["selected"])

        volume = self.snapshot.volume(volume_id)
        stored = SessionStore(self.root).load(volume.ref)
        self.assertEqual(
            stored.selected_paths,
            {self.snapshot.page(first_page_id).ref.relative_path},
        )

        positioned = self.json_request(
            "PUT",
            f"/api/volume/{volume_id}/position",
            {"page_id": second_page_id},
            credential=self.credential,
            client_id=self.CLIENT_ID,
        )
        self.assertEqual(positioned.status, 200)
        self.assertEqual(
            self.payload(positioned)["review"]["current_page_id"],
            second_page_id,
        )
        self.assertEqual(SessionStore(self.root).load(volume.ref).current_index, 1)

        missing_route = self.api.handle(
            "POST",
            "/api/export",
            self.headers(
                credential=self.credential,
                client_id=self.CLIENT_ID,
                content_type="application/json",
                origin=self.ORIGIN,
            ),
            b"{}",
        )
        self.assertEqual(missing_route.status, 404)

    def test_images_are_authenticated_validated_and_privately_cacheable(self) -> None:
        self.assertEqual(self.claim().status, 200)
        volume_id = self.snapshot.mangas[0].volume_ids[0]
        first_page_id, second_page_id = self.snapshot.volume(volume_id).page_ids
        authenticated = self.headers(
            credential=self.credential,
            client_id=self.CLIENT_ID,
        )

        unauthorized = self.api.handle(
            "GET",
            f"/api/page/{first_page_id}/image",
            self.headers(client_id=self.CLIENT_ID),
        )
        self.assertEqual(unauthorized.status, 401)

        for page_id, expected_type, expected_body in (
            (first_page_id, "image/jpeg", b"jpg-one"),
            (second_page_id, "image/png", b"png-two"),
        ):
            with self.subTest(page_id=page_id):
                response = self.api.handle(
                    "GET", f"/api/page/{page_id}/image", authenticated
                )
                headers = dict(response.headers)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read_body(), expected_body)
                self.assertEqual(headers["Content-Type"], expected_type)
                self.assertEqual(headers["Content-Length"], str(len(expected_body)))
                self.assertIn("private", headers["Cache-Control"])
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

                conditional = self.api.handle(
                    "GET",
                    f"/api/page/{page_id}/image",
                    {**authenticated, "If-None-Match": headers["ETag"]},
                )
                self.assertEqual(conditional.status, 304)
                self.assertEqual(conditional.body, b"")

        forged = self.api.handle(
            "GET", "/api/page/p_forged/image", authenticated
        )
        self.assertEqual(forged.status, 404)
        self.assertEqual(
            self.payload(forged)["error"]["code"],
            "stale_snapshot",
        )

    def test_host_origin_and_json_content_type_fail_closed(self) -> None:
        self.assertEqual(self.claim().status, 200)
        volume_id = self.snapshot.mangas[0].volume_ids[0]
        page_id = self.snapshot.volume(volume_id).page_ids[0]

        missing_host = self.api.handle("GET", "/api/status", {})
        public_host = self.api.handle(
            "GET", "/api/status", {"Host": "attacker.example:8787"}
        )
        cross_origin = self.json_request(
            "PUT",
            f"/api/volume/{volume_id}/position",
            {"page_id": page_id},
            credential=self.credential,
            client_id=self.CLIENT_ID,
            origin="http://attacker.example:8787",
        )
        wrong_scheme = self.json_request(
            "PUT",
            f"/api/volume/{volume_id}/position",
            {"page_id": page_id},
            credential=self.credential,
            client_id=self.CLIENT_ID,
            origin="https://desktop.local:8787",
        )
        wrong_content_type = self.json_request(
            "PUT",
            f"/api/volume/{volume_id}/position",
            {"page_id": page_id},
            credential=self.credential,
            client_id=self.CLIENT_ID,
            content_type="text/plain",
        )

        self.assertEqual(missing_host.status, 403)
        self.assertEqual(public_host.status, 403)
        self.assertEqual(cross_origin.status, 403)
        self.assertEqual(wrong_scheme.status, 403)
        self.assertEqual(wrong_content_type.status, 415)

        allowed_lan_host = self.api.handle(
            "GET", "/api/status", {"Host": "192.168.1.50:8787"}
        )
        self.assertEqual(allowed_lan_host.status, 200)
        self.assertFalse(
            any(
                name.casefold() == "access-control-allow-origin"
                for name, _value in allowed_lan_host.headers
            )
        )

    def test_missing_live_page_and_save_failure_do_not_confirm_a_write(self) -> None:
        self.assertEqual(self.claim().status, 200)
        volume_id = self.snapshot.mangas[0].volume_ids[0]
        page_id = self.snapshot.volume(volume_id).page_ids[0]
        self.first_page.unlink()

        missing_page = self.json_request(
            "PUT",
            f"/api/volume/{volume_id}/selection",
            {"page_id": page_id, "selected": True},
            credential=self.credential,
            client_id=self.CLIENT_ID,
        )

        self.assertEqual(missing_page.status, 404)
        self.assertEqual(
            self.payload(missing_page)["error"]["code"],
            "missing_image",
        )
        self.assertFalse(
            SessionStore(self.root)
            .path_for(self.snapshot.volume(volume_id).ref)
            .exists()
        )

        self.first_page.write_bytes(b"jpg-one")
        with patch(
            "pocket_manga_editor.companion.review.SessionStore.save",
            side_effect=OSError("disk unavailable"),
        ):
            failed_save = self.json_request(
                "PUT",
                f"/api/volume/{volume_id}/selection",
                {"page_id": page_id, "selected": True},
                credential=self.credential,
                client_id=self.CLIENT_ID,
            )

        self.assertEqual(failed_save.status, 503)
        self.assertEqual(
            self.payload(failed_save)["error"]["code"],
            "save_failure",
        )
        self.assertEqual(
            self.coordinator.status().state,
            CompanionState.COMPANION_ERROR,
        )
        self.assertFalse(self.coordinator.status().active_client)
        with self.assertRaises(DesktopMutationBlocked):
            self.coordinator.require_desktop_mutation()

    def test_pair_route_sets_strict_cookie_but_does_not_activate_mode(self) -> None:
        credential = "new-paired-device-credential"
        pairing = PairingManager(
            code_factory=lambda: "445566",
            credential_factory=lambda: credential,
        )
        coordinator = CompanionCoordinator(pairing_manager=pairing)
        coordinator.start_pairing()
        api = CompanionAPI(coordinator, allowed_hosts={"desktop.local"})

        response = api.handle(
            "POST",
            "/api/pair",
            {
                "Host": self.HOST,
                "Origin": self.ORIGIN,
                "Content-Type": "application/json",
            },
            b'{"code":"445566"}',
        )

        self.assertEqual(response.status, 200)
        self.assertNotIn(credential, response.body.decode("utf-8"))
        cookie = dict(response.headers)["Set-Cookie"]
        self.assertIn(f"{COOKIE_NAME}={credential}", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertFalse(coordinator.status().state is CompanionState.COMPANION_ACTIVE)

        inactive_claim = api.handle(
            "POST",
            "/api/controller/claim",
            {
                "Host": self.HOST,
                "Origin": self.ORIGIN,
                "Content-Type": "application/json",
                "Cookie": f"{COOKIE_NAME}={credential}",
            },
            b'{"client_id":"phone","page_id":"inactive-page"}',
        )
        self.assertEqual(inactive_claim.status, 409)
        self.assertEqual(
            self.payload(inactive_claim)["error"]["code"],
            "inactive_mode",
        )


class CompanionHTTPServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.services: list[CompanionHTTPService] = []

    def tearDown(self) -> None:
        for service in self.services:
            service.stop()

    def service(self, port: int) -> CompanionHTTPService:
        service = CompanionHTTPService(
            CompanionCoordinator(),
            host="127.0.0.1",
            port=port,
            public_host="127.0.0.1",
        )
        self.services.append(service)
        return service

    def test_streaming_body_honors_declared_length_and_detects_truncation(self) -> None:
        grown = APIResponse(200, (), StreamingBody(io.BytesIO(b"abcdef"), 3))
        self.assertEqual(grown.read_body(), b"abc")

        truncated = APIResponse(200, (), StreamingBody(io.BytesIO(b"abc"), 6))
        with self.assertRaises(OSError):
            truncated.read_body()

    def test_server_start_stop_and_restart_own_the_worker_lifecycle(self) -> None:
        class FakeHTTPServer:
            instances: list[FakeHTTPServer] = []

            def __init__(self, address, handler) -> None:
                self.address = address
                self.handler = handler
                self.serving = threading.Event()
                self.shutdown_requested = threading.Event()
                self.closed = False
                self.instances.append(self)

            def serve_forever(self, poll_interval: float) -> None:
                self.serving.set()
                self.shutdown_requested.wait(2)

            def shutdown(self) -> None:
                self.shutdown_requested.set()

            def server_close(self) -> None:
                self.closed = True

        port = 48123
        service = self.service(port)

        with patch(
            "pocket_manga_editor.companion.server._CompanionHTTPServer",
            FakeHTTPServer,
        ):
            started = service.start()

            self.assertTrue(started.running)
            self.assertEqual(started.url, f"http://127.0.0.1:{port}/")
            self.assertEqual(len(FakeHTTPServer.instances), 1)
            backend = FakeHTTPServer.instances[0]
            self.assertEqual(backend.address, ("127.0.0.1", port))
            self.assertTrue(backend.serving.wait(1))
            self.assertTrue(service.start().running)
            self.assertEqual(len(FakeHTTPServer.instances), 1)

            stopped = service.stop()
            self.assertFalse(stopped.running)
            self.assertTrue(backend.closed)
            self.assertTrue(service.restart().running)
            self.assertEqual(len(FakeHTTPServer.instances), 2)
            self.assertFalse(service.stop().running)

    def test_real_loopback_listener_serves_status_and_static_shell(self) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
        except PermissionError:
            self.skipTest("The test sandbox does not permit loopback listeners.")
        service = self.service(port)
        started = service.start()
        self.assertTrue(started.running, started.error)

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", "/api/status")
            response = connection.getresponse()
            status_payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertFalse(status_payload["status"]["companion_active"])
            self.assertFalse(status_payload["status"]["paired"])

            connection.request("GET", "/")
            response = connection.getresponse()
            shell = response.read()
            self.assertEqual(response.status, 200)
            self.assertIn(b"Pocket Manga Editor", shell)
            self.assertIn(
                b'/assets/app.js?v=page-lease-v2',
                shell,
            )
            self.assertIn(
                "default-src 'self'",
                response.getheader("Content-Security-Policy") or "",
            )
            self.assertIn(
                "blob:",
                response.getheader("Content-Security-Policy") or "",
            )
            self.assertEqual(response.getheader("X-Frame-Options"), "DENY")

            connection.request("GET", "/assets/app.js?v=page-lease-v2")
            response = connection.getresponse()
            javascript = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Cache-Control"), "no-cache")
            self.assertIn(b"X-Companion-Page", javascript)

            with tempfile.TemporaryDirectory() as source_directory:
                source_root = Path(source_directory)
                source_page = source_root / "Streamed" / "Vol. 01" / "001.jpg"
                source_page.parent.mkdir(parents=True)
                source_page.write_bytes(b"streamed-image-body")
                coordinator = service.coordinator
                offer = coordinator.start_pairing()
                credential = coordinator.pair(offer.code)
                snapshot = coordinator.enter_companion(
                    source_root, scan_working_directory(source_root)
                )
                coordinator.claim_controller(credential, "phone", "loopback-page")
                volume_id = snapshot.mangas[0].volume_ids[0]
                page_id = snapshot.volume(volume_id).page_ids[0]
                connection.request(
                    "GET",
                    f"/api/page/{page_id}/image",
                    headers={
                        "Cookie": f"{COOKIE_NAME}={credential}",
                        "X-Companion-Instance": "phone",
                        "X-Companion-Page": "loopback-page",
                    },
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b"streamed-image-body")
        finally:
            connection.close()

        self.assertFalse(service.stop().running)

    def test_port_conflict_is_reported_without_affecting_desktop_authority(self) -> None:
        coordinator = CompanionCoordinator()
        service = CompanionHTTPService(
            coordinator,
            host="127.0.0.1",
            port=48124,
            public_host="127.0.0.1",
        )
        self.services.append(service)
        with patch(
            "pocket_manga_editor.companion.server._CompanionHTTPServer",
            side_effect=OSError("Address already in use"),
        ):
            status = service.start()

        self.assertFalse(status.running)
        self.assertIn("Could not listen", status.error or "")
        self.assertEqual(
            coordinator.status().state,
            CompanionState.DESKTOP_ACTIVE,
        )
        coordinator.require_desktop_mutation()


if __name__ == "__main__":
    unittest.main()
