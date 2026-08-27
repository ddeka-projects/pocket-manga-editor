from __future__ import annotations

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
    CompanionAPI,
    StreamingBody,
)
from pocket_manga_editor.companion.coordinator import CompanionCoordinator
from pocket_manga_editor.companion.lease import (
    ControllerLease,
    LeaseConflictError,
    LeaseError,
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
    OperationBusyError,
    RescanError,
    WrongActivityError,
)
from pocket_manga_editor.scanner import ScanError, scan_working_directory
from pocket_manga_editor.storage import EditingStore, ReadingSnapshot, ReadingStore
from pocket_manga_editor.workspace import manga_workspace_paths


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


class ControllerLeaseTests(unittest.TestCase):
    def test_one_exact_page_owns_and_renews_the_lease(self) -> None:
        clock = FakeClock()
        lease = ControllerLease(ttl_seconds=15, clock=clock)
        claimed = lease.claim("browser", "page-one")
        self.assertTrue(claimed.connected)
        self.assertEqual(claimed.lease_expires_at, 1_015)
        with self.assertRaises(LeaseConflictError):
            lease.claim("browser", "page-two")
        with self.assertRaises(LeaseConflictError):
            lease.claim("other-browser", "page-one")
        clock.advance(5)
        self.assertEqual(
            lease.heartbeat("browser", "page-one").lease_expires_at,
            1_020,
        )

    def test_any_page_can_claim_immediately_after_expiry_or_release(self) -> None:
        clock = FakeClock()
        lease = ControllerLease(ttl_seconds=15, clock=clock)
        lease.claim("first", "page")
        clock.advance(15)
        self.assertEqual(lease.claim("second", "page").instance_id, "second")
        lease.release("second", "page")
        self.assertEqual(lease.claim("third", "page").instance_id, "third")
        lease.release("third", "page")
        lease.release("third", "page")

    def test_expired_wrong_and_invalid_identifiers_fail_closed(self) -> None:
        clock = FakeClock()
        lease = ControllerLease(ttl_seconds=15, clock=clock)
        lease.claim("browser", "page")
        with self.assertRaises(LeaseConflictError):
            lease.authorize("browser", "other-page")
        clock.advance(15)
        with self.assertRaises(LeaseExpiredError):
            lease.authorize("browser", "page")
        with self.assertRaises(LeaseError):
            lease.claim("bad id", "page")
        with self.assertRaises(LeaseError):
            lease.claim("browser", "")


class LibrarySnapshotTests(CompanionFixture):
    def test_ids_are_opaque_scoped_and_follow_manga_folder_image(self) -> None:
        first = self.build_snapshot(token_prefix="alpha")
        second = self.build_snapshot(token_prefix="beta")
        manga = first.mangas[0]
        folder_id = manga.folder_ids[0]
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
        self.assertEqual((reading.last_folder, reading.last_image), (folder.ref.name, "2.PNG"))
        self.assertEqual((editing.last_folder, editing.last_image), (folder.ref.name, "10.jpg"))
        self.assertEqual(
            editing.folders[folder.ref.name].selected_images,
            frozenset({"1.jpg"}),
        )
        self.assertEqual(selection.manga_selected_count, 1)

    def test_only_latest_manga_position_resumes(self) -> None:
        snapshot = self.build_snapshot()
        manga = snapshot.mangas[0]
        first_folder = snapshot.folder(manga.folder_ids[0])
        second_folder = snapshot.folder(manga.folder_ids[1])
        review = ReviewService(snapshot)
        review.set_position(
            CompanionActivity.EDIT, first_folder.id, first_folder.image_ids[-1]
        )
        review.set_position(
            CompanionActivity.EDIT, second_folder.id, second_folder.image_ids[0]
        )
        manga_payload = review.manga_payload(manga.id, CompanionActivity.EDIT)["manga"]
        first_payload = review.folder_payload(
            first_folder.id, CompanionActivity.EDIT
        )["folder"]
        self.assertEqual(manga_payload["current_folder_id"], second_folder.id)
        self.assertEqual(manga_payload["current_image_id"], second_folder.image_ids[0])
        self.assertTrue(
            all("current_image_id" not in item for item in manga_payload["folders"])
        )
        self.assertEqual(first_payload["current_image_id"], first_folder.image_ids[0])

    def test_read_payload_has_no_paths_or_selection_semantics(self) -> None:
        snapshot = self.build_snapshot()
        manga = snapshot.mangas[0]
        payload = ReviewService(snapshot).folder_payload(
            manga.folder_ids[0], CompanionActivity.READ
        )
        serialized = json.dumps(payload)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("selected", serialized)
        self.assertNotIn("exports", serialized)

    def test_storage_warnings_do_not_expose_paths(self) -> None:
        snapshot = self.build_snapshot()
        manga = snapshot.mangas[0]
        loaded = ReadingSnapshot(
            manga.ref.folders[0].name,
            manga.ref.folders[0].images[0].name,
            (f"Could not read {self.root}/private/reading.json",),
        )
        with patch.object(ReadingStore, "load", return_value=loaded):
            payload = ReviewService(snapshot).manga_payload(
                manga.id, CompanionActivity.READ
            )
        self.assertNotIn(str(self.root), json.dumps(payload))
        self.assertEqual(len(payload["warnings"]), 1)

    def test_read_selection_is_never_persisted(self) -> None:
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


class CoordinatorTests(CompanionFixture):
    CLIENT = "browser"
    PAGE = "page"

    def setUp(self) -> None:
        super().setUp()
        self.coordinator = CompanionCoordinator(self.root, self.scan_result)
        self.coordinator.claim_controller(self.CLIENT, self.PAGE)

    def test_always_active_coordinator_persists_review_and_keeps_lease_on_error(self) -> None:
        snapshot_id = self.coordinator.status().snapshot_id
        library = self.coordinator.library(self.CLIENT, self.PAGE)
        self.assertEqual(library["snapshot_id"], snapshot_id)
        manga_id = library["mangas"][0]["id"]
        manga = self.coordinator.open_manga(
            self.CLIENT, manga_id, CompanionActivity.EDIT, self.PAGE
        )["manga"]
        folder_id = manga["folders"][0]["id"]
        image_id = self.coordinator.folder(
            self.CLIENT, folder_id, CompanionActivity.EDIT, self.PAGE
        )["folder"]["images"][0]["id"]
        with patch.object(EditingStore, "set_selection", side_effect=OSError("disk")):
            with self.assertRaises(ReviewSaveError):
                self.coordinator.set_selection(
                    self.CLIENT,
                    CompanionActivity.EDIT,
                    folder_id,
                    image_id,
                    True,
                    self.PAGE,
                )
        self.assertTrue(self.coordinator.status().active_client)
        self.assertEqual(self.coordinator.status().snapshot_id, snapshot_id)

    def test_rescan_swaps_only_a_complete_snapshot_and_retains_controller(self) -> None:
        original = self.coordinator.status().snapshot_id
        self.add_image("Series Three", "Chapter 1", "new.jpg", b"new")
        with (
            patch(
                "pocket_manga_editor.companion.coordinator.LibrarySnapshot.build",
                side_effect=SnapshotError("bad snapshot"),
            ),
            patch("pocket_manga_editor.companion.coordinator.LOGGER.exception"),
        ):
            with self.assertRaises(RescanError):
                self.coordinator.rescan(self.CLIENT, self.PAGE)
        self.assertEqual(self.coordinator.status().snapshot_id, original)
        self.assertEqual(len(self.coordinator.library(self.CLIENT, self.PAGE)["mangas"]), 2)

        library = self.coordinator.rescan(self.CLIENT, self.PAGE)
        self.assertNotEqual(library["snapshot_id"], original)
        self.assertEqual(len(library["mangas"]), 3)
        self.assertTrue(self.coordinator.status().active_client)

    def test_long_operation_blocks_writes_but_not_heartbeat(self) -> None:
        library = self.coordinator.library(self.CLIENT, self.PAGE)
        manga_id = library["mangas"][0]["id"]
        manga = self.coordinator.open_manga(
            self.CLIENT, manga_id, CompanionActivity.EDIT, self.PAGE
        )["manga"]
        folder_id = manga["folders"][0]["id"]
        image_id = self.coordinator.folder(
            self.CLIENT, folder_id, CompanionActivity.EDIT, self.PAGE
        )["folder"]["images"][0]["id"]
        started = threading.Event()
        finish = threading.Event()

        def blocked_preview(*_args):
            started.set()
            self.assertTrue(finish.wait(2))
            return type(
                "Preview",
                (),
                {
                    "selected_folder_count": 0,
                    "selected_image_count": 0,
                    "output_exists": False,
                    "unrecognized_entries": (),
                },
            )()

        failures: list[BaseException] = []

        def inspect() -> None:
            try:
                self.coordinator.export_preview(self.CLIENT, manga_id, self.PAGE)
            except BaseException as exc:
                failures.append(exc)

        with patch(
            "pocket_manga_editor.companion.coordinator.exporter_module.inspect_export",
            side_effect=blocked_preview,
        ):
            thread = threading.Thread(target=inspect)
            thread.start()
            self.assertTrue(started.wait(1))
            self.assertTrue(
                self.coordinator.heartbeat_controller(self.CLIENT, self.PAGE).connected
            )
            with self.assertRaises(OperationBusyError):
                self.coordinator.set_selection(
                    self.CLIENT,
                    CompanionActivity.EDIT,
                    folder_id,
                    image_id,
                    True,
                    self.PAGE,
                )
            finish.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])


class CompanionAPITests(CompanionFixture):
    HOST = "desktop.local:8787"
    ORIGIN = "http://desktop.local:8787"
    CLIENT = "browser-home-screen"
    PAGE = "page-instance-one"

    def setUp(self) -> None:
        super().setUp()
        self.coordinator = CompanionCoordinator(self.root, self.scan_result)
        self.api = CompanionAPI(self.coordinator, allowed_hosts={"desktop.local"})

    @staticmethod
    def payload(response: APIResponse) -> dict[str, object]:
        return json.loads(response.read_body())

    def headers(
        self,
        *,
        controller: bool = True,
        content_type: str | None = None,
        origin: str | None = None,
        host: str | None = None,
    ) -> dict[str, str]:
        headers = {"Host": host or self.HOST}
        if controller:
            headers["X-Companion-Instance"] = self.CLIENT
            headers["X-Companion-Page"] = self.PAGE
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
        controller: bool = True,
    ) -> APIResponse:
        return self.api.handle(
            method,
            target,
            self.headers(
                controller=controller,
                content_type="application/json; charset=utf-8",
                origin=self.ORIGIN,
            ),
            json.dumps(payload).encode(),
            client_address="192.168.1.25",
        )

    def claim(self, client: str | None = None, page: str | None = None) -> APIResponse:
        return self.json_request(
            "POST",
            "/api/controller/claim",
            {"client_id": client or self.CLIENT, "page_id": page or self.PAGE},
            controller=False,
        )

    def get(self, target: str) -> APIResponse:
        return self.api.handle(
            "GET",
            target,
            self.headers(),
            client_address="192.168.1.25",
        )

    def open_activity(self, activity: str = "read") -> dict[str, object]:
        manga_id = self.payload(self.get("/api/library"))["mangas"][0]["id"]
        response = self.get(f"/api/manga/{manga_id}?activity={activity}")
        self.assertEqual(response.status, 200)
        return self.payload(response)

    def test_status_and_claim_are_open_but_every_library_route_needs_lease(self) -> None:
        status = self.api.handle(
            "GET",
            "/api/status",
            {"Host": self.HOST},
            client_address="127.0.0.1",
        )
        self.assertEqual(status.status, 200)
        self.assertEqual(self.payload(status)["status"], {"server": "available"})
        unclaimed = self.get("/api/library")
        self.assertEqual(unclaimed.status, 409)
        self.assertEqual(self.payload(unclaimed)["error"]["code"], "lease_expired")
        self.assertEqual(self.claim().status, 200)
        self.assertEqual(self.get("/api/library").status, 200)
        self.assertEqual(
            self.json_request("POST", "/api/pair", {}, controller=False).status,
            404,
        )

    def test_claim_heartbeat_contention_release_and_exact_bodies(self) -> None:
        malformed = self.json_request(
            "POST",
            "/api/controller/claim",
            {"client_id": self.CLIENT, "page_id": self.PAGE, "extra": True},
            controller=False,
        )
        self.assertEqual(malformed.status, 400)
        claimed = self.claim()
        self.assertEqual(claimed.status, 200)
        self.assertEqual(self.payload(claimed)["controller"]["page_id"], self.PAGE)
        blocked = self.claim("second-browser", "second-page")
        self.assertEqual(blocked.status, 423)
        self.assertEqual(self.payload(blocked)["error"]["code"], "lease_conflict")
        heartbeat = self.json_request(
            "POST",
            "/api/controller/heartbeat",
            {"client_id": self.CLIENT, "page_id": self.PAGE},
            controller=False,
        )
        self.assertEqual(heartbeat.status, 200)
        released = self.json_request(
            "POST",
            "/api/controller/release",
            {"client_id": self.CLIENT, "page_id": self.PAGE},
            controller=False,
        )
        self.assertEqual(released.status, 200)
        self.assertEqual(self.get("/api/library").status, 409)

    def test_protected_routes_require_both_controller_headers(self) -> None:
        self.assertEqual(self.claim().status, 200)
        response = self.api.handle(
            "GET",
            "/api/library",
            {"Host": self.HOST, "X-Companion-Instance": self.CLIENT},
            client_address="192.168.1.25",
        )
        self.assertEqual(response.status, 400)

    def test_activity_queries_and_patch_writes_are_strict_and_independent(self) -> None:
        self.assertEqual(self.claim().status, 200)
        library = self.payload(self.get("/api/library"))
        self.assertNotIn("selected_count", library["mangas"][0])
        manga_id = library["mangas"][0]["id"]
        for query in ("", "?activity=", "?activity=read&activity=edit", "?mode=read"):
            with self.subTest(query=query):
                self.assertEqual(self.get(f"/api/manga/{manga_id}{query}").status, 400)

        read_manga = self.payload(
            self.get(f"/api/manga/{manga_id}?activity=read")
        )["manga"]
        folder_id = read_manga["folders"][0]["id"]
        read_folder = self.payload(
            self.get(f"/api/folder/{folder_id}?activity=read")
        )["folder"]
        self.assertNotIn("selected", json.dumps(read_folder))
        second_id = read_folder["images"][1]["id"]
        positioned = self.json_request(
            "PATCH",
            f"/api/read/folder/{folder_id}/position",
            {"image_id": second_id},
        )
        self.assertEqual(positioned.status, 200)
        self.assertEqual(
            self.json_request(
                "PUT",
                f"/api/read/folder/{folder_id}/position",
                {"image_id": second_id},
            ).status,
            404,
        )

        self.get(f"/api/manga/{manga_id}?activity=edit")
        edit_folder = self.payload(
            self.get(f"/api/folder/{folder_id}?activity=edit")
        )["folder"]
        first_id = edit_folder["images"][0]["id"]
        tenth_id = edit_folder["images"][2]["id"]
        self.assertEqual(
            self.json_request(
                "PATCH",
                f"/api/edit/folder/{folder_id}/position",
                {"image_id": tenth_id},
            ).status,
            200,
        )
        selected = self.json_request(
            "PATCH",
            f"/api/edit/folder/{folder_id}/selection",
            {"image_id": first_id, "selected": True},
        )
        self.assertEqual(selected.status, 200)
        manga_ref = self.scan_result.mangas[0]
        folder_name = manga_ref.folders[0].name
        self.assertEqual(ReadingStore(self.root).load(manga_ref).last_image, "2.PNG")
        editing = EditingStore(self.root).load(manga_ref)
        self.assertEqual(editing.last_image, "10.jpg")
        self.assertEqual(
            editing.folders[folder_name].selected_images, frozenset({"1.jpg"})
        )

    def test_activity_rebind_rejects_stale_writes(self) -> None:
        self.assertEqual(self.claim().status, 200)
        read = self.open_activity("read")["manga"]
        folder_id = read["folders"][0]["id"]
        image_id = self.payload(
            self.get(f"/api/folder/{folder_id}?activity=read")
        )["folder"]["images"][0]["id"]
        manga_id = read["id"]
        self.get(f"/api/manga/{manga_id}?activity=edit")
        stale = self.json_request(
            "PATCH",
            f"/api/read/folder/{folder_id}/position",
            {"image_id": image_id},
        )
        self.assertEqual(stale.status, 409)
        self.assertEqual(self.payload(stale)["error"]["code"], "wrong_activity")

    def test_images_are_lease_protected_validated_and_privately_cacheable(self) -> None:
        self.assertEqual(self.claim().status, 200)
        manga = self.open_activity("read")["manga"]
        folder_id = manga["folders"][0]["id"]
        folder = self.payload(
            self.get(f"/api/folder/{folder_id}?activity=read")
        )["folder"]
        image_id = folder["images"][0]["id"]
        response = self.get(f"/api/image/{image_id}")
        headers = dict(response.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read_body(), b"jpg-one")
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        self.assertIn("private", headers["Cache-Control"])
        conditional = self.api.handle(
            "GET",
            f"/api/image/{image_id}",
            {**self.headers(), "If-None-Match": headers["ETag"]},
            client_address="192.168.1.25",
        )
        self.assertEqual(conditional.status, 304)

        self.first_image.unlink()
        missing = self.get(f"/api/image/{image_id}")
        self.assertEqual(missing.status, 404)
        self.assertEqual(self.payload(missing)["error"]["code"], "missing_image")

    def test_host_origin_content_type_and_peer_address_fail_closed(self) -> None:
        missing_host = self.api.handle(
            "GET", "/api/status", {}, client_address="127.0.0.1"
        )
        public_host = self.api.handle(
            "GET",
            "/api/status",
            {"Host": "attacker.example:8787"},
            client_address="127.0.0.1",
        )
        public_peer = self.api.handle(
            "GET",
            "/api/status",
            {"Host": self.HOST},
            client_address="8.8.8.8",
        )
        allowed_lan = self.api.handle(
            "GET",
            "/api/status",
            {"Host": "192.168.1.50:8787"},
            client_address="192.168.1.25",
        )
        self.assertEqual(missing_host.status, 403)
        self.assertEqual(public_host.status, 403)
        self.assertEqual(public_peer.status, 403)
        self.assertEqual(allowed_lan.status, 200)

        cross_origin = self.api.handle(
            "POST",
            "/api/controller/claim",
            self.headers(
                controller=False,
                content_type="application/json",
                origin="http://attacker.example",
            ),
            json.dumps({"client_id": self.CLIENT, "page_id": self.PAGE}).encode(),
            client_address="192.168.1.25",
        )
        wrong_type = self.api.handle(
            "POST",
            "/api/controller/claim",
            self.headers(
                controller=False,
                content_type="text/plain",
                origin=self.ORIGIN,
            ),
            json.dumps({"client_id": self.CLIENT, "page_id": self.PAGE}).encode(),
            client_address="192.168.1.25",
        )
        self.assertEqual(cross_origin.status, 403)
        self.assertEqual(wrong_type.status, 415)

    def test_export_refuses_zero_then_warns_and_atomically_replaces_output(self) -> None:
        self.assertEqual(self.claim().status, 200)
        library = self.payload(self.get("/api/library"))
        manga_id = library["mangas"][0]["id"]
        preview = self.payload(
            self.get(f"/api/manga/{manga_id}/export-preview")
        )["export"]
        self.assertEqual(preview["selected_image_count"], 0)
        refused = self.json_request(
            "POST",
            f"/api/manga/{manga_id}/export",
            {"confirm_unrecognized_output": False},
        )
        self.assertEqual(refused.status, 409)
        self.assertEqual(self.payload(refused)["error"]["code"], "nothing_selected")

        edit = self.payload(
            self.get(f"/api/manga/{manga_id}?activity=edit")
        )["manga"]
        folder_id = edit["folders"][0]["id"]
        folder = self.payload(
            self.get(f"/api/folder/{folder_id}?activity=edit")
        )["folder"]
        image_id = folder["images"][0]["id"]
        self.assertEqual(
            self.json_request(
                "PATCH",
                f"/api/edit/folder/{folder_id}/selection",
                {"image_id": image_id, "selected": True},
            ).status,
            200,
        )
        manga_ref = self.scan_result.mangas[0]
        workspace = manga_workspace_paths(self.root, manga_ref.name)
        workspace.output.mkdir(parents=True)
        outsider = workspace.output / "test.txt"
        outsider.write_text("outside", encoding="utf-8")
        warning = self.payload(
            self.get(f"/api/manga/{manga_id}/export-preview")
        )["export"]
        self.assertTrue(warning["requires_confirmation"])
        self.assertIn("test.txt", warning["unrecognized_entries"])

        unconfirmed = self.json_request(
            "POST",
            f"/api/manga/{manga_id}/export",
            {"confirm_unrecognized_output": False},
        )
        self.assertEqual(unconfirmed.status, 409)
        self.assertEqual(
            self.payload(unconfirmed)["error"]["code"],
            "export_confirmation_required",
        )
        self.assertTrue(outsider.exists())
        exported = self.json_request(
            "POST",
            f"/api/manga/{manga_id}/export",
            {"confirm_unrecognized_output": True},
        )
        self.assertEqual(exported.status, 200, exported.body)
        result = self.payload(exported)["export"]
        self.assertEqual(result["selected_image_count"], 1)
        self.assertFalse(outsider.exists())

    def test_rescan_returns_new_library_keeps_lease_and_retains_old_on_failure(self) -> None:
        claimed = self.payload(self.claim())
        previous = claimed["snapshot_id"]
        self.add_image("Series Three", "New", "1.jpg", b"new")
        rescanned = self.json_request("POST", "/api/library/rescan", {})
        self.assertEqual(rescanned.status, 200)
        payload = self.payload(rescanned)
        self.assertTrue(payload["rescanned"])
        self.assertNotEqual(payload["snapshot_id"], previous)
        self.assertEqual(len(payload["mangas"]), 3)
        stable = payload["snapshot_id"]

        with (
            patch(
                "pocket_manga_editor.companion.coordinator.scan_working_directory",
                side_effect=ScanError("broken source"),
            ),
            patch("pocket_manga_editor.companion.coordinator.LOGGER.exception"),
        ):
            failed = self.json_request("POST", "/api/library/rescan", {})
        self.assertEqual(failed.status, 503)
        self.assertEqual(self.payload(failed)["error"]["code"], "rescan_failed")
        current = self.payload(self.get("/api/library"))
        self.assertEqual(current["snapshot_id"], stable)
        extra = self.json_request(
            "POST", "/api/library/rescan", {"unexpected": True}
        )
        self.assertEqual(extra.status, 400)

    def test_old_destructive_and_pairing_routes_are_absent(self) -> None:
        self.assertEqual(self.claim().status, 200)
        for method, route in (
            ("GET", "/api/volume/v_old"),
            ("GET", "/api/page/p_old/image"),
            ("POST", "/api/export"),
            ("POST", "/api/complete"),
            ("POST", "/api/pair"),
        ):
            with self.subTest(route=route):
                response = (
                    self.json_request(method, route, {})
                    if method == "POST"
                    else self.get(route)
                )
                self.assertEqual(response.status, 404)


class CompanionHTTPServiceTests(CompanionFixture):
    def setUp(self) -> None:
        super().setUp()
        self.services: list[CompanionHTTPService] = []

    def tearDown(self) -> None:
        for service in self.services:
            service.stop()
        super().tearDown()

    def service(self, port: int) -> CompanionHTTPService:
        service = CompanionHTTPService(
            CompanionCoordinator(self.root, self.scan_result),
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

    def test_real_listener_serves_versioned_assets_and_rejects_put(self) -> None:
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
            self.assertIn(b"/assets/app.js?v=always-on-web-v1", shell)
            self.assertIn(b"/assets/styles.css?v=always-on-web-v1", shell)
            self.assertIn(
                "default-src 'self'",
                response.getheader("Content-Security-Policy") or "",
            )
            connection.request(
                "PUT",
                "/api/library/rescan",
                body=b"{}",
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 405)
        finally:
            connection.close()

    def test_port_conflict_preserves_the_ready_coordinator(self) -> None:
        service = self.service(48124)
        snapshot_id = service.coordinator.status().snapshot_id
        with patch(
            "pocket_manga_editor.companion.server._CompanionHTTPServer",
            side_effect=OSError("Address already in use"),
        ):
            status = service.start()
        self.assertFalse(status.running)
        self.assertIn("Could not listen", status.error or "")
        self.assertEqual(service.coordinator.status().snapshot_id, snapshot_id)


if __name__ == "__main__":
    unittest.main()
