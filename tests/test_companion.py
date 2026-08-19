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
from pocket_manga_editor.companion.review import ReviewSaveError, ReviewService
from pocket_manga_editor.companion.server import CompanionHTTPService
from pocket_manga_editor.companion.snapshot import (
    LibrarySnapshot,
    SnapshotError,
    SnapshotLookupError,
)
from pocket_manga_editor.companion.state import (
    CompanionActivity,
    CompanionState,
    CompanionStateError,
    DesktopMutationBlocked,
    MobileAccessError,
    ShutdownTransitionError,
    WrongActivityError,
    validate_transition,
)
from pocket_manga_editor.scanner import scan_working_directory
from pocket_manga_editor.storage import (
    EditingStore,
    ReadingFolderState,
    ReadingSnapshot,
    ReadingStore,
)


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
        self.first_image = self.add_image(
            "Series One", "Part III - Ch 21", "1.jpg", b"jpg-one"
        )
        self.second_image = self.add_image(
            "Series One", "Part III - Ch 21", "2.PNG", b"png-two"
        )
        self.tenth_image = self.add_image(
            "Series One", "Part III - Ch 21", "10.jpg", b"jpg-ten"
        )
        self.other_folder_image = self.add_image(
            "Series One", "Volume Two - Ch 12", "cover.JPG", b"cover"
        )
        self.other_manga_image = self.add_image(
            "Series Two", "Bonus scans", "scan-a.png", b"scan"
        )
        self.scan_result = scan_working_directory(self.root)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def add_image(
        self, manga: str, folder: str, filename: str, content: bytes
    ) -> Path:
        image = self.root / manga / folder / filename
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(content)
        return image

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
        credential = manager.pair(manager.open_pairing().code)
        raw = self.store.path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        self.assertEqual(credential, self.credential)
        self.assertNotIn(self.credential, raw)
        self.assertEqual(
            payload["credential_verifier"],
            hashlib.sha256(self.credential.encode()).hexdigest(),
        )
        restarted = self.manager()
        restarted.authorize(self.credential)
        with self.assertRaises(AuthenticationError):
            restarted.authorize("wrong")

    def test_pairing_expiry_and_attempt_limit_remain_enforced(self) -> None:
        manager = self.manager()
        manager.open_pairing(ttl_seconds=10, max_attempts=2)
        with self.assertRaises(InvalidPairingCodeError):
            manager.pair("000000")
        with self.assertRaises(PairingRateLimitedError):
            manager.pair("000000")
        with self.assertRaises(PairingClosedError):
            manager.pair("123456")

    def test_failed_persistent_revocation_invalidates_live_credential(self) -> None:
        manager = self.manager()
        manager.open_pairing()
        manager.pair("123456")
        with patch.object(self.store, "clear", side_effect=OSError("read only")):
            with self.assertRaises(OSError):
                manager.forget()
        self.assertTrue(manager.revocation_pending)
        with self.assertRaises(AuthenticationError):
            manager.authorize(self.credential)
        manager.forget()
        self.assertFalse(manager.revocation_pending)
        self.assertFalse(manager.paired)


class ControllerLeaseTests(unittest.TestCase):
    def test_one_page_instance_owns_and_reclaims_the_lease(self) -> None:
        clock = FakeClock()
        lease = ControllerLease(ttl_seconds=5, grace_seconds=10, clock=clock)
        lease.claim("browser", "page-one")
        with self.assertRaises(LeaseConflictError):
            lease.claim("browser", "page-two")
        clock.advance(6)
        with self.assertRaises(LeaseExpiredError):
            lease.authorize("browser", "page-one")
        self.assertTrue(lease.claim("browser", "page-two").connected)

    def test_different_client_waits_for_full_grace(self) -> None:
        clock = FakeClock()
        lease = ControllerLease(ttl_seconds=5, grace_seconds=10, clock=clock)
        lease.claim("first", "page")
        clock.advance(6)
        with self.assertRaises(LeaseConflictError):
            lease.claim("second", "page")
        clock.advance(10)
        self.assertEqual(lease.claim("second", "page").instance_id, "second")


class LibrarySnapshotTests(CompanionFixture):
    def test_ids_are_opaque_scoped_and_follow_manga_folder_image(self) -> None:
        first = self.build_snapshot(token_prefix="alpha")
        second = self.build_snapshot(token_prefix="beta")
        manga = first.mangas[0]
        folder_id = manga.folder_ids[0]
        image_id = first.folder(folder_id).image_ids[0]
        public_ids = {
            first.snapshot_id,
            *(entry.id for entry in first.mangas),
            *(folder for entry in first.mangas for folder in entry.folder_ids),
            *(
                image
                for entry in first.mangas
                for folder in entry.folder_ids
                for image in first.folder(folder).image_ids
            ),
        }
        for value in public_ids:
            self.assertNotIn("Series", value)
            self.assertNotIn("Part", value)
            self.assertNotIn("/", value)
            self.assertNotIn(str(self.root), value)
        with self.assertRaises(SnapshotLookupError):
            second.manga(manga.id)
        with self.assertRaises(SnapshotLookupError):
            second.folder(folder_id)
        with self.assertRaises(SnapshotLookupError):
            second.image(image_id)

    def test_image_ids_cannot_cross_folder_boundaries(self) -> None:
        snapshot = self.build_snapshot()
        manga = snapshot.mangas[0]
        first_folder, second_folder = manga.folder_ids
        image = snapshot.folder(first_folder).image_ids[0]
        self.assertEqual(snapshot.image_in_folder(first_folder, image).id, image)
        with self.assertRaises(SnapshotLookupError):
            snapshot.image_in_folder(second_folder, image)

    def test_snapshot_rejects_symlinked_folder_after_scan(self) -> None:
        source = self.first_image.parent
        relocated = self.root / "relocated"
        source.rename(relocated)
        try:
            source.symlink_to(relocated, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory symlinks are unavailable: {exc}")
        with self.assertRaises(SnapshotError):
            LibrarySnapshot.build(self.root, self.scan_result)


class ReviewPersistenceTests(CompanionFixture):
    def test_library_is_neutral_and_default_reads_create_no_metadata(self) -> None:
        snapshot = self.build_snapshot()
        review = ReviewService(snapshot)
        manga = snapshot.mangas[0]
        with (
            patch.object(ReadingStore, "load") as read_load,
            patch.object(EditingStore, "load") as edit_load,
        ):
            library = review.library_payload()
        read_load.assert_not_called()
        edit_load.assert_not_called()
        read_payload = review.manga_payload(manga.id, CompanionActivity.READ)
        edit_payload = review.manga_payload(manga.id, CompanionActivity.EDIT)
        self.assertNotIn("selected_count", library["mangas"][0])
        self.assertNotIn("selected_count", read_payload["manga"])
        self.assertIn("selected_count", edit_payload["manga"])
        self.assertFalse(ReadingStore(self.root).path_for(manga.ref).exists())
        self.assertFalse(EditingStore(self.root).path_for(manga.ref).exists())

    def test_read_and_edit_positions_and_selections_are_independent(self) -> None:
        snapshot = self.build_snapshot()
        manga = snapshot.mangas[0]
        folder_id = manga.folder_ids[0]
        folder = snapshot.folder(folder_id)
        first_id, second_id, tenth_id = folder.image_ids
        review = ReviewService(snapshot)
        review.set_position(CompanionActivity.READ, folder_id, second_id)
        review.set_position(CompanionActivity.EDIT, folder_id, tenth_id)
        selection = review.set_selection(
            CompanionActivity.EDIT, folder_id, first_id, True
        )
        reading = ReadingStore(self.root).load(manga.ref)
        editing = EditingStore(self.root).load(manga.ref)
        self.assertEqual(reading.folders[folder.ref.name].current_image, "2.PNG")
        self.assertEqual(editing.folders[folder.ref.name].current_image, "10.jpg")
        self.assertEqual(
            editing.folders[folder.ref.name].selected_images, frozenset({"1.jpg"})
        )
        self.assertEqual(selection.manga_selected_count, 1)

    def test_read_payload_contains_no_paths_or_selection_semantics(self) -> None:
        snapshot = self.build_snapshot()
        manga = snapshot.mangas[0]
        folder_id = manga.folder_ids[0]
        payload = ReviewService(snapshot).folder_payload(
            folder_id, CompanionActivity.READ
        )
        serialized = json.dumps(payload)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("selected", serialized)
        self.assertNotIn("exports", serialized)
        self.assertTrue(
            all(
                image["image_url"].startswith("/api/image/i_")
                for image in payload["folder"]["images"]
            )
        )

    def test_mobile_warnings_never_expose_storage_error_paths(self) -> None:
        snapshot = self.build_snapshot()
        manga = snapshot.mangas[0]
        folder_states = {
            folder.name: ReadingFolderState(folder.images[0].name)
            for folder in manga.ref.folders
        }
        loaded = ReadingSnapshot(
            manga.ref.folders[0].name,
            folder_states,
            (f"Could not read {self.root}/private/reading.json",),
        )
        with patch.object(ReadingStore, "load", return_value=loaded):
            payload = ReviewService(snapshot).manga_payload(
                manga.id, CompanionActivity.READ
            )
        serialized = json.dumps(payload)
        self.assertNotIn(str(self.root), serialized)
        self.assertEqual(len(payload["warnings"]), 1)

    def test_forced_read_selection_never_calls_editing_store(self) -> None:
        snapshot = self.build_snapshot()
        manga = snapshot.mangas[0]
        folder_id = manga.folder_ids[0]
        image_id = snapshot.folder(folder_id).image_ids[0]
        review = ReviewService(snapshot)
        with patch.object(EditingStore, "set_selection") as save:
            with self.assertRaises(WrongActivityError):
                review.set_selection(
                    CompanionActivity.READ, folder_id, image_id, True
                )
        save.assert_not_called()
        self.assertFalse(EditingStore(self.root).path_for(manga.ref).exists())

    def test_selection_of_another_image_does_not_regress_position(self) -> None:
        snapshot = self.build_snapshot()
        manga = snapshot.mangas[0]
        folder_id = manga.folder_ids[0]
        first_id, _second_id, tenth_id = snapshot.folder(folder_id).image_ids
        review = ReviewService(snapshot)
        review.set_position(CompanionActivity.EDIT, folder_id, tenth_id)
        review.set_selection(CompanionActivity.EDIT, folder_id, first_id, True)
        editing = EditingStore(self.root).load(manga.ref)
        self.assertEqual(
            editing.folders[snapshot.folder(folder_id).ref.name].current_image,
            "10.jpg",
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
        self.coordinator = CompanionCoordinator(pairing_manager=pairing)

    def activate(self):
        snapshot = self.coordinator.enter_companion(self.root, self.scan_result)
        self.coordinator.claim_controller(self.credential, "phone", "page")
        return snapshot

    def test_activity_context_and_ownership_transfer(self) -> None:
        self.coordinator.require_desktop_mutation()
        snapshot = self.activate()
        with self.assertRaises(DesktopMutationBlocked):
            self.coordinator.require_desktop_mutation()
        manga = snapshot.mangas[0]
        self.coordinator.open_manga(
            self.credential,
            "phone",
            manga.id,
            CompanionActivity.READ,
            "page",
        )
        read_context = self.coordinator.status().mobile_context
        self.assertEqual(read_context.activity, CompanionActivity.READ)
        self.assertIsNone(read_context.selected_count)
        self.coordinator.open_manga(
            self.credential,
            "phone",
            manga.id,
            CompanionActivity.EDIT,
            "page",
        )
        folder_id = manga.folder_ids[0]
        image_id = snapshot.folder(folder_id).image_ids[0]
        self.coordinator.set_selection(
            self.credential,
            "phone",
            CompanionActivity.EDIT,
            folder_id,
            image_id,
            True,
            "page",
        )
        context = self.coordinator.begin_exit()
        self.assertEqual(context.selected_count, 1)
        with self.assertRaises(ShutdownTransitionError):
            self.coordinator.library(self.credential, "phone", "page")
        self.coordinator.finish_exit()
        self.coordinator.require_desktop_mutation()

    def test_error_state_remains_fail_closed_until_two_phase_recovery(self) -> None:
        self.coordinator.fail("uncertain save")
        status = self.coordinator.status()
        self.assertEqual(status.state, CompanionState.COMPANION_ERROR)
        self.assertEqual(status.last_error, "uncertain save")
        with self.assertRaises(DesktopMutationBlocked):
            self.coordinator.require_desktop_mutation()
        with self.assertRaises(MobileAccessError):
            self.coordinator.claim_controller(self.credential, "phone", "page")

        self.coordinator.begin_recovery()
        with self.assertRaises(DesktopMutationBlocked):
            self.coordinator.require_desktop_mutation()
        with self.assertRaises(MobileAccessError):
            self.coordinator.claim_controller(self.credential, "phone", "page")
        self.coordinator.finish_recovery()
        self.assertEqual(
            self.coordinator.status().state,
            CompanionState.DESKTOP_ACTIVE,
        )
        self.coordinator.require_desktop_mutation()

    def test_read_save_failure_does_not_fail_companion(self) -> None:
        snapshot = self.activate()
        manga = snapshot.mangas[0]
        self.coordinator.open_manga(
            self.credential, "phone", manga.id, CompanionActivity.READ, "page"
        )
        folder_id = manga.folder_ids[0]
        image_id = snapshot.folder(folder_id).image_ids[1]
        with patch.object(ReadingStore, "set_position", side_effect=OSError("disk")):
            with self.assertRaises(ReviewSaveError):
                self.coordinator.set_position(
                    self.credential,
                    "phone",
                    CompanionActivity.READ,
                    folder_id,
                    image_id,
                    "page",
                )
        self.assertEqual(
            self.coordinator.status().state, CompanionState.COMPANION_ACTIVE
        )
        self.assertTrue(self.coordinator.status().active_client)

    def test_edit_save_failure_is_fail_closed(self) -> None:
        snapshot = self.activate()
        manga = snapshot.mangas[0]
        self.coordinator.open_manga(
            self.credential, "phone", manga.id, CompanionActivity.EDIT, "page"
        )
        folder_id = manga.folder_ids[0]
        image_id = snapshot.folder(folder_id).image_ids[0]
        with patch.object(EditingStore, "set_selection", side_effect=OSError("disk")):
            with self.assertRaises(ReviewSaveError):
                self.coordinator.set_selection(
                    self.credential,
                    "phone",
                    CompanionActivity.EDIT,
                    folder_id,
                    image_id,
                    True,
                    "page",
                )
        self.assertEqual(self.coordinator.status().state, CompanionState.COMPANION_ERROR)
        self.assertFalse(self.coordinator.status().active_client)
        with self.assertRaises(DesktopMutationBlocked):
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
        self.coordinator = CompanionCoordinator(pairing_manager=pairing)
        self.snapshot = self.coordinator.enter_companion(self.root, self.scan_result)
        self.api = CompanionAPI(self.coordinator, allowed_hosts={"desktop.local"})

    @staticmethod
    def payload(response) -> dict[str, object]:
        return json.loads(response.body.decode("utf-8"))

    def headers(
        self,
        *,
        credential: str | None = None,
        client_id: str | None = None,
        content_type: str | None = None,
        origin: str | None = None,
        host: str | None = None,
    ) -> dict[str, str]:
        headers = {"Host": host or self.HOST}
        if credential is not None:
            headers["Cookie"] = f"{COOKIE_NAME}={credential}"
        if client_id is not None:
            headers["X-Companion-Instance"] = client_id
            headers["X-Companion-Page"] = self.PAGE_INSTANCE_ID
        if content_type is not None:
            headers["Content-Type"] = content_type
        if origin is not None:
            headers["Origin"] = origin
        return headers

    def json_request(self, method: str, target: str, payload: dict[str, object]):
        return self.api.handle(
            method,
            target,
            self.headers(
                credential=self.credential,
                client_id=self.CLIENT_ID,
                content_type="application/json; charset=utf-8",
                origin=self.ORIGIN,
            ),
            json.dumps(payload).encode(),
        )

    def claim(self):
        return self.json_request(
            "POST",
            "/api/controller/claim",
            {"client_id": self.CLIENT_ID, "page_id": self.PAGE_INSTANCE_ID},
        )

    def authenticated(self):
        return self.headers(credential=self.credential, client_id=self.CLIENT_ID)

    def open_activity(self, activity: str = "read"):
        manga_id = self.snapshot.mangas[0].id
        response = self.api.handle(
            "GET",
            f"/api/manga/{manga_id}?activity={activity}",
            self.authenticated(),
        )
        self.assertEqual(response.status, 200, response.body)
        return self.payload(response)

    def test_status_public_but_library_requires_auth_and_lease(self) -> None:
        status = self.api.handle("GET", "/api/status", {"Host": self.HOST})
        self.assertEqual(status.status, 200)
        self.assertNotIn("Series One", status.body.decode())
        unauthorized = self.api.handle(
            "GET", "/api/library", self.headers(client_id=self.CLIENT_ID)
        )
        self.assertEqual(unauthorized.status, 401)
        unclaimed = self.api.handle(
            "GET", "/api/library", self.authenticated()
        )
        self.assertEqual(unclaimed.status, 409)

    def test_claim_heartbeat_contention_and_release_enforce_one_controller(self) -> None:
        claimed = self.claim()
        self.assertEqual(claimed.status, 200)
        self.assertEqual(
            self.payload(claimed)["snapshot_id"], self.snapshot.snapshot_id
        )
        blocked = self.json_request(
            "POST",
            "/api/controller/claim",
            {"client_id": "second-phone", "page_id": "second-page"},
        )
        self.assertEqual(blocked.status, 423)
        self.assertEqual(self.payload(blocked)["error"]["code"], "lease_conflict")

        heartbeat = self.json_request(
            "POST",
            "/api/controller/heartbeat",
            {"client_id": self.CLIENT_ID, "page_id": self.PAGE_INSTANCE_ID},
        )
        self.assertEqual(heartbeat.status, 200)
        released = self.json_request(
            "POST",
            "/api/controller/release",
            {"client_id": self.CLIENT_ID, "page_id": self.PAGE_INSTANCE_ID},
        )
        self.assertEqual(released.status, 200)
        after_release = self.api.handle(
            "GET", "/api/library", self.authenticated()
        )
        self.assertEqual(after_release.status, 409)
        self.assertEqual(
            self.payload(after_release)["error"]["code"], "lease_expired"
        )

    def test_authenticated_routes_require_the_page_nonce_header(self) -> None:
        self.assertEqual(self.claim().status, 200)
        response = self.api.handle(
            "GET",
            "/api/library",
            {
                "Host": self.HOST,
                "Cookie": f"{COOKIE_NAME}={self.credential}",
                "X-Companion-Instance": self.CLIENT_ID,
            },
        )
        self.assertEqual(response.status, 400)

    def test_pair_sets_a_strict_cookie_without_activating_companion(self) -> None:
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
        self.assertEqual(coordinator.status().state, CompanionState.DESKTOP_ACTIVE)

        inactive = api.handle(
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
        self.assertEqual(inactive.status, 409)
        self.assertEqual(self.payload(inactive)["error"]["code"], "inactive_mode")

    def test_controller_and_mutation_bodies_have_exact_clean_break_shapes(self) -> None:
        legacy_claim = self.json_request(
            "POST",
            "/api/controller/claim",
            {"instance_id": self.CLIENT_ID, "page_id": self.PAGE_INSTANCE_ID},
        )
        self.assertEqual(legacy_claim.status, 400)
        extra_claim = self.json_request(
            "POST",
            "/api/controller/claim",
            {
                "client_id": self.CLIENT_ID,
                "page_id": self.PAGE_INSTANCE_ID,
                "instance_id": self.CLIENT_ID,
            },
        )
        self.assertEqual(extra_claim.status, 400)
        self.assertEqual(self.claim().status, 200)

        for route in ("heartbeat", "release"):
            with self.subTest(route=route):
                response = self.json_request(
                    "POST",
                    f"/api/controller/{route}",
                    {
                        "client_id": self.CLIENT_ID,
                        "page_id": self.PAGE_INSTANCE_ID,
                        "unexpected": True,
                    },
                )
                self.assertEqual(response.status, 400)

        read_manga = self.open_activity("read")["manga"]
        folder_id = read_manga["folders"][0]["id"]
        read_folder = self.payload(
            self.api.handle(
                "GET",
                f"/api/folder/{folder_id}?activity=read",
                self.authenticated(),
            )
        )["folder"]
        image_id = read_folder["images"][0]["id"]
        read_position = self.json_request(
            "PUT",
            f"/api/read/folder/{folder_id}/position",
            {"image_id": image_id, "unexpected": True},
        )
        self.assertEqual(read_position.status, 400)

        self.open_activity("edit")
        edit_position = self.json_request(
            "PUT",
            f"/api/edit/folder/{folder_id}/position",
            {"image_id": image_id, "unexpected": True},
        )
        self.assertEqual(edit_position.status, 400)
        edit_selection = self.json_request(
            "PUT",
            f"/api/edit/folder/{folder_id}/selection",
            {"image_id": image_id, "selected": True, "unexpected": True},
        )
        self.assertEqual(edit_selection.status, 400)

    def test_activity_neutral_library_and_strict_activity_query(self) -> None:
        self.assertEqual(self.claim().status, 200)
        library_response = self.api.handle(
            "GET", "/api/library", self.authenticated()
        )
        library = self.payload(library_response)
        self.assertNotIn("selected_count", library["mangas"][0])
        manga_id = library["mangas"][0]["id"]
        for query in ("", "?activity=", "?activity=read&activity=edit", "?mode=read"):
            with self.subTest(query=query):
                response = self.api.handle(
                    "GET", f"/api/manga/{manga_id}{query}", self.authenticated()
                )
                self.assertEqual(response.status, 400)

    def test_read_has_no_selection_fields_and_rejects_edit_write(self) -> None:
        self.assertEqual(self.claim().status, 200)
        manga = self.open_activity("read")["manga"]
        folder_id = manga["folders"][0]["id"]
        folder_response = self.api.handle(
            "GET",
            f"/api/folder/{folder_id}?activity=read",
            self.authenticated(),
        )
        body = folder_response.body.decode()
        folder = self.payload(folder_response)["folder"]
        self.assertNotIn("selected", body)
        self.assertNotIn("exports", body)
        image_id = folder["images"][0]["id"]
        rejected = self.json_request(
            "PUT",
            f"/api/edit/folder/{folder_id}/selection",
            {"image_id": image_id, "selected": True},
        )
        self.assertEqual(rejected.status, 409)
        self.assertEqual(self.payload(rejected)["error"]["code"], "wrong_activity")
        self.assertFalse(
            EditingStore(self.root).path_for(self.snapshot.mangas[0].ref).exists()
        )

    def test_read_and_edit_routes_persist_independent_state(self) -> None:
        self.assertEqual(self.claim().status, 200)
        read_manga = self.open_activity("read")["manga"]
        folder_id = read_manga["folders"][0]["id"]
        read_folder = self.payload(
            self.api.handle(
                "GET",
                f"/api/folder/{folder_id}?activity=read",
                self.authenticated(),
            )
        )["folder"]
        second_id = read_folder["images"][1]["id"]
        positioned = self.json_request(
            "PUT",
            f"/api/read/folder/{folder_id}/position",
            {"image_id": second_id},
        )
        self.assertEqual(positioned.status, 200)

        edit_manga = self.open_activity("edit")["manga"]
        edit_folder = self.payload(
            self.api.handle(
                "GET",
                f"/api/folder/{folder_id}?activity=edit",
                self.authenticated(),
            )
        )["folder"]
        tenth_id = edit_folder["images"][2]["id"]
        first_id = edit_folder["images"][0]["id"]
        self.assertEqual(
            self.json_request(
                "PUT",
                f"/api/edit/folder/{folder_id}/position",
                {"image_id": tenth_id},
            ).status,
            200,
        )
        selected = self.json_request(
            "PUT",
            f"/api/edit/folder/{folder_id}/selection",
            {"image_id": first_id, "selected": True},
        )
        self.assertEqual(selected.status, 200)
        self.assertEqual(self.payload(selected)["selection"]["manga_selected_count"], 1)
        manga_ref = self.snapshot.mangas[0].ref
        folder_name = self.snapshot.folder(folder_id).ref.name
        self.assertEqual(
            ReadingStore(self.root).load(manga_ref).folders[folder_name].current_image,
            "2.PNG",
        )
        editing = EditingStore(self.root).load(manga_ref).folders[folder_name]
        self.assertEqual(editing.current_image, "10.jpg")
        self.assertEqual(editing.selected_images, frozenset({"1.jpg"}))

    def test_activity_rebind_rejects_queued_writes_from_the_old_activity(self) -> None:
        self.assertEqual(self.claim().status, 200)
        manga_ref = self.snapshot.mangas[0].ref

        read_manga = self.open_activity("read")["manga"]
        folder_id = read_manga["folders"][0]["id"]
        read_folder = self.payload(
            self.api.handle(
                "GET",
                f"/api/folder/{folder_id}?activity=read",
                self.authenticated(),
            )
        )["folder"]
        image_id = read_folder["images"][1]["id"]

        self.open_activity("edit")
        stale_read = self.json_request(
            "PUT",
            f"/api/read/folder/{folder_id}/position",
            {"image_id": image_id},
        )
        self.assertEqual(stale_read.status, 409)
        self.assertEqual(
            self.payload(stale_read)["error"]["code"], "wrong_activity"
        )
        self.assertFalse(ReadingStore(self.root).path_for(manga_ref).exists())

        self.open_activity("read")
        stale_edit = self.json_request(
            "PUT",
            f"/api/edit/folder/{folder_id}/selection",
            {"image_id": image_id, "selected": True},
        )
        self.assertEqual(stale_edit.status, 409)
        self.assertEqual(
            self.payload(stale_edit)["error"]["code"], "wrong_activity"
        )
        self.assertFalse(EditingStore(self.root).path_for(manga_ref).exists())

    def test_old_volume_page_and_mobile_destructive_routes_are_absent(self) -> None:
        self.assertEqual(self.claim().status, 200)
        for method, route in (
            ("GET", "/api/volume/v_old"),
            ("GET", "/api/page/p_old/image"),
            ("POST", "/api/export"),
            ("POST", "/api/complete"),
        ):
            with self.subTest(route=route):
                response = self.api.handle(
                    method,
                    route,
                    self.headers(
                        credential=self.credential,
                        client_id=self.CLIENT_ID,
                        content_type="application/json",
                        origin=self.ORIGIN,
                    ),
                    b"{}" if method == "POST" else b"",
                )
                self.assertEqual(response.status, 404)

    def test_images_are_authenticated_validated_and_privately_cacheable(self) -> None:
        self.assertEqual(self.claim().status, 200)
        manga = self.open_activity("read")["manga"]
        folder_id = manga["folders"][0]["id"]
        folder = self.payload(
            self.api.handle(
                "GET",
                f"/api/folder/{folder_id}?activity=read",
                self.authenticated(),
            )
        )["folder"]
        image_id = folder["images"][0]["id"]
        unauthorized = self.api.handle(
            "GET", f"/api/image/{image_id}", self.headers(client_id=self.CLIENT_ID)
        )
        self.assertEqual(unauthorized.status, 401)
        response = self.api.handle(
            "GET", f"/api/image/{image_id}", self.authenticated()
        )
        headers = dict(response.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read_body(), b"jpg-one")
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        self.assertIn("private", headers["Cache-Control"])
        conditional = self.api.handle(
            "GET",
            f"/api/image/{image_id}",
            {**self.authenticated(), "If-None-Match": headers["ETag"]},
        )
        self.assertEqual(conditional.status, 304)

    def test_host_origin_and_content_type_still_fail_closed(self) -> None:
        self.assertEqual(self.claim().status, 200)
        manga = self.open_activity("read")["manga"]
        folder_id = manga["folders"][0]["id"]
        image_id = self.snapshot.folder(folder_id).image_ids[0]
        missing_host = self.api.handle("GET", "/api/status", {})
        public_host = self.api.handle(
            "GET", "/api/status", {"Host": "attacker.example:8787"}
        )
        cross_origin = self.api.handle(
            "PUT",
            f"/api/read/folder/{folder_id}/position",
            self.headers(
                credential=self.credential,
                client_id=self.CLIENT_ID,
                content_type="application/json",
                origin="http://attacker.example",
            ),
            json.dumps({"image_id": image_id}).encode(),
        )
        wrong_scheme = self.api.handle(
            "PUT",
            f"/api/read/folder/{folder_id}/position",
            self.headers(
                credential=self.credential,
                client_id=self.CLIENT_ID,
                content_type="application/json",
                origin="https://desktop.local:8787",
            ),
            json.dumps({"image_id": image_id}).encode(),
        )
        wrong_type = self.api.handle(
            "PUT",
            f"/api/read/folder/{folder_id}/position",
            self.headers(
                credential=self.credential,
                client_id=self.CLIENT_ID,
                content_type="text/plain",
                origin=self.ORIGIN,
            ),
            json.dumps({"image_id": image_id}).encode(),
        )
        self.assertEqual(missing_host.status, 403)
        self.assertEqual(public_host.status, 403)
        self.assertEqual(cross_origin.status, 403)
        self.assertEqual(wrong_scheme.status, 403)
        self.assertEqual(wrong_type.status, 415)
        allowed_lan = self.api.handle(
            "GET", "/api/status", {"Host": "192.168.1.50:8787"}
        )
        self.assertEqual(allowed_lan.status, 200)
        self.assertFalse(
            any(
                name.casefold() == "access-control-allow-origin"
                for name, _value in allowed_lan.headers
            )
        )

    def test_missing_live_image_and_edit_save_failure_do_not_confirm(self) -> None:
        self.assertEqual(self.claim().status, 200)
        manga = self.open_activity("edit")["manga"]
        folder_id = manga["folders"][0]["id"]
        image_id = self.snapshot.folder(folder_id).image_ids[0]
        self.first_image.unlink()
        missing = self.json_request(
            "PUT",
            f"/api/edit/folder/{folder_id}/selection",
            {"image_id": image_id, "selected": True},
        )
        self.assertEqual(missing.status, 404)
        self.assertEqual(self.payload(missing)["error"]["code"], "missing_image")
        self.first_image.write_bytes(b"jpg-one")
        with patch.object(EditingStore, "set_selection", side_effect=OSError("disk")):
            failed = self.json_request(
                "PUT",
                f"/api/edit/folder/{folder_id}/selection",
                {"image_id": image_id, "selected": True},
            )
        self.assertEqual(failed.status, 503)
        self.assertEqual(self.payload(failed)["error"]["code"], "save_failure")
        self.assertEqual(self.coordinator.status().state, CompanionState.COMPANION_ERROR)


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

    def test_streaming_body_honors_length_and_detects_truncation(self) -> None:
        grown = APIResponse(200, (), StreamingBody(io.BytesIO(b"abcdef"), 3))
        self.assertEqual(grown.read_body(), b"abc")
        truncated = APIResponse(200, (), StreamingBody(io.BytesIO(b"abc"), 6))
        with self.assertRaises(OSError):
            truncated.read_body()

    def test_server_start_stop_and_restart_own_worker_lifecycle(self) -> None:
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

        service = self.service(48123)
        with patch(
            "pocket_manga_editor.companion.server._CompanionHTTPServer",
            FakeHTTPServer,
        ):
            self.assertTrue(service.start().running)
            backend = FakeHTTPServer.instances[0]
            self.assertTrue(backend.serving.wait(1))
            self.assertTrue(service.start().running)
            self.assertEqual(len(FakeHTTPServer.instances), 1)
            self.assertFalse(service.stop().running)
            self.assertTrue(backend.closed)
            self.assertTrue(service.restart().running)
            self.assertEqual(len(FakeHTTPServer.instances), 2)

    def test_real_listener_versions_both_protocol_assets(self) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
        except PermissionError:
            self.skipTest("The test sandbox does not permit loopback listeners.")
        service = self.service(port)
        started = service.start()
        if not started.running:
            self.skipTest(started.error or "Loopback listener unavailable")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            shell = response.read()
            self.assertEqual(response.status, 200)
            self.assertIn(b"/assets/app.js?v=filesystem-activity-v1", shell)
            self.assertIn(b"/assets/styles.css?v=filesystem-activity-v1", shell)
            self.assertIn(
                "default-src 'self'",
                response.getheader("Content-Security-Policy") or "",
            )
            connection.request("GET", "/assets/styles.css?v=filesystem-activity-v1")
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Cache-Control"), "no-cache")
        finally:
            connection.close()

    def test_port_conflict_does_not_change_desktop_authority(self) -> None:
        service = self.service(48124)
        with patch(
            "pocket_manga_editor.companion.server._CompanionHTTPServer",
            side_effect=OSError("Address already in use"),
        ):
            status = service.start()
        self.assertFalse(status.running)
        self.assertIn("Could not listen", status.error or "")
        self.assertEqual(
            service.coordinator.status().state,
            CompanionState.DESKTOP_ACTIVE,
        )
        service.coordinator.require_desktop_mutation()


if __name__ == "__main__":
    unittest.main()
