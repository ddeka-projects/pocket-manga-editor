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

from pocket_manga_editor.api import (
    APIResponse,
    API,
    StreamingBody,
)
from pocket_manga_editor.service import LibraryService
from pocket_manga_editor.health import make_health
from pocket_manga_editor.controller import (
    ControllerError,
    ControllerOccupiedError,
    ControllerOwnership,
    ControllerRequiredError,
    ControllerTransferredError,
)
from pocket_manga_editor.review import ReviewSaveError, ReviewService
from pocket_manga_editor.server import HTTPService
from pocket_manga_editor.snapshot import (
    LibraryIndex,
    MangaSnapshot,
    MissingImageError,
    SnapshotError,
    SnapshotLookupError,
)
from pocket_manga_editor.activity import (
    Activity,
    MangaUnavailableError,
    RescanError,
    WrongActivityError,
)
from pocket_manga_editor.scanner import ScanError, scan_library, scan_manga
from pocket_manga_editor.storage import EditingStore, ReadingStore


class ServerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.first_image = self.add_image("Series One", "Part III - Ch 21", "1.jpg", b"jpg-one")
        self.second_image = self.add_image("Series One", "Part III - Ch 21", "2.PNG", b"png-two")
        self.tenth_image = self.add_image("Series One", "Part III - Ch 21", "10.jpg", b"jpg-ten")
        self.other_folder_image = self.add_image(
            "Series One", "Volume Two - Ch 12", "cover.JPG", b"cover"
        )
        self.other_manga_image = self.add_image("Series Two", "Bonus scans", "scan-a.png", b"scan")
        self.scan_result = scan_library(self.root)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def add_image(self, manga: str, folder: str, filename: str, content: bytes) -> Path:
        image = self.root / manga / folder / filename
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(content)
        return image

    def build_manga(self, name: str = "Series One") -> MangaSnapshot:
        return MangaSnapshot.build(self.root.resolve(), "m_test", scan_manga(self.root, name).manga)


class ControllerOwnershipTests(unittest.TestCase):
    def test_one_page_owns_until_explicit_release_or_takeover(self) -> None:
        ownership = ControllerOwnership()
        first = ownership.claim("page-one")
        self.assertTrue(ownership.snapshot()["claimed"])
        with ownership.authorized("page-one", first["token"]):
            pass
        with self.assertRaises(ControllerOccupiedError):
            ownership.claim("page-two")

        second = ownership.claim("page-two", takeover=True)
        with self.assertRaises(ControllerTransferredError):
            with ownership.authorized("page-one", first["token"]):
                pass
        with ownership.authorized("page-two", second["token"]):
            pass

        ownership.release("page-two", second["token"])
        with self.assertRaises(ControllerRequiredError):
            with ownership.authorized("page-two", second["token"]):
                pass

    def test_reclaim_rotates_token_and_invalid_identifiers_fail_closed(self) -> None:
        ownership = ControllerOwnership()
        first = ownership.claim("same-page")
        second = ownership.claim("same-page")
        self.assertNotEqual(first["token"], second["token"])
        with self.assertRaises(ControllerTransferredError):
            with ownership.authorized("same-page", first["token"]):
                pass
        with self.assertRaises(ControllerError):
            ownership.claim("bad id")
        with self.assertRaises(ControllerError):
            ownership.claim("")


class SnapshotTests(ServerFixture):
    def test_ids_are_opaque_and_replaced_by_each_scan(self) -> None:
        library = LibraryIndex.build(self.scan_result)
        first = self.build_manga()
        second = self.build_manga()
        public_ids = {
            library.snapshot_id,
            *(entry.id for entry in library.mangas),
            *first.folder_ids,
            *(image for folder in first.folders for image in folder.image_ids),
        }
        for value in public_ids:
            self.assertNotIn("Series", value)
            self.assertNotIn("Part", value)
            self.assertNotIn("/", value)
            self.assertNotIn(str(self.root), value)
        self.assertEqual([entry.name for entry in library.mangas], ["Series One", "Series Two"])
        with self.assertRaises(SnapshotLookupError):
            LibraryIndex.build(self.scan_result).manga(library.mangas[0].id)
        with self.assertRaises(SnapshotLookupError):
            second.folder(first.folder_ids[0])

    def test_image_ids_cannot_cross_folder_boundaries(self) -> None:
        manga = self.build_manga()
        first_folder, second_folder = manga.folder_ids
        image = manga.folder(first_folder).image_ids[0]
        self.assertEqual(manga.image_in_folder(first_folder, image).id, image)
        with self.assertRaises(SnapshotLookupError):
            manga.image_in_folder(second_folder, image)

    def test_live_validation_rejects_a_folder_swapped_for_a_link(self) -> None:
        manga = self.build_manga()
        image_id = manga.folder(manga.folder_ids[0]).image_ids[0]
        source = self.first_image.parent
        relocated = self.root / "relocated"
        source.rename(relocated)
        try:
            source.symlink_to(relocated, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory symlinks are unavailable: {exc}")
        with self.assertRaises(MissingImageError):
            manga.validate_live_image(image_id)


class ReviewPersistenceTests(ServerFixture):
    def test_default_payloads_create_no_metadata_and_carry_no_counts(self) -> None:
        manga = self.build_manga()
        review = ReviewService(manga)
        read_payload = review.manga_payload(Activity.READ)
        edit_payload = review.manga_payload(Activity.EDIT)
        edit_folder = review.folder_payload(manga.folder_ids[0], Activity.EDIT)
        self.assertNotIn("count", json.dumps([read_payload, edit_payload, edit_folder]))
        self.assertEqual(edit_payload["manga"]["current_folder_id"], manga.folder_ids[0])
        self.assertFalse(ReadingStore(self.root).path_for(manga.ref).exists())
        self.assertFalse(EditingStore(self.root).path_for(manga.ref).exists())

    def test_read_and_edit_positions_and_selections_are_independent(self) -> None:
        manga = self.build_manga()
        folder_id = manga.folder_ids[0]
        folder = manga.folder(folder_id)
        first_id, second_id, tenth_id = folder.image_ids
        review = ReviewService(manga)
        review.set_position(Activity.READ, folder_id, second_id)
        review.set_position(Activity.EDIT, folder_id, tenth_id)
        selection = review.set_selection(Activity.EDIT, folder_id, first_id, True)
        reading = ReadingStore(self.root).load(manga.ref)
        editing = EditingStore(self.root).load(manga.ref)
        self.assertEqual((reading.last_folder, reading.last_image), (folder.ref.name, "2.PNG"))
        self.assertEqual(
            (editing.position.last_folder, editing.position.last_image),
            (folder.ref.name, "10.jpg"),
        )
        self.assertEqual(editing.folders[folder.ref.name], ("1.jpg",))
        self.assertTrue(selection.selected)

    def test_only_latest_manga_position_resumes(self) -> None:
        manga = self.build_manga()
        first_folder = manga.folder(manga.folder_ids[0])
        second_folder = manga.folder(manga.folder_ids[1])
        review = ReviewService(manga)
        review.set_position(Activity.EDIT, first_folder.id, first_folder.image_ids[-1])
        review.set_position(Activity.EDIT, second_folder.id, second_folder.image_ids[0])
        manga_payload = review.manga_payload(Activity.EDIT)["manga"]
        first_payload = review.folder_payload(first_folder.id, Activity.EDIT)["folder"]
        self.assertEqual(manga_payload["current_folder_id"], second_folder.id)
        self.assertEqual(manga_payload["current_image_id"], second_folder.image_ids[0])
        self.assertTrue(all("current_image_id" not in item for item in manga_payload["folders"]))
        self.assertEqual(first_payload["current_image_id"], first_folder.image_ids[0])

    def test_resume_falls_back_to_the_entry_start_then_the_manga_start(self) -> None:
        reading = ReadingStore(self.root).path_for(scan_manga(self.root, "Series One").manga)
        reading.parent.mkdir(parents=True)
        cases = (
            ("Volume Two - Ch 12", "cover.JPG", "Volume Two - Ch 12", "cover.JPG"),
            ("Part III - Ch 21", "missing.jpg", "Part III - Ch 21", "1.jpg"),
            ("Missing Entry", "1.jpg", "Part III - Ch 21", "1.jpg"),
        )
        for saved_folder, saved_image, folder_name, image_name in cases:
            with self.subTest(saved=(saved_folder, saved_image)):
                reading.write_text(
                    json.dumps({"last_folder": saved_folder, "last_image": saved_image}),
                    encoding="utf-8",
                )
                manga = self.build_manga()
                payload = ReviewService(manga).manga_payload(Activity.READ)["manga"]
                folder = manga.folder(payload["current_folder_id"])
                image = manga.image(payload["current_image_id"])
                self.assertEqual((folder.ref.name, image.ref.name), (folder_name, image_name))
        self.assertIn("Missing Entry", reading.read_text())

    def test_missing_selections_are_hidden_but_never_pruned(self) -> None:
        # Day 1: 1.jpg, 2.PNG and 10.jpg were selected. Day 2: 2.PNG is gone.
        editing = EditingStore(self.root).path_for(scan_manga(self.root, "Series One").manga)
        editing.parent.mkdir(parents=True)
        editing.write_text(
            json.dumps(
                {
                    "last_folder": "Part III - Ch 21",
                    "last_image": "1.jpg",
                    "folders": {
                        "Old Entry": {"selected_images": ["old.jpg"]},
                        "Part III - Ch 21": {"selected_images": ["1.jpg", "2.PNG", "10.jpg"]},
                    },
                }
            ),
            encoding="utf-8",
        )
        self.second_image.unlink()
        manga = self.build_manga()
        review = ReviewService(manga)
        folder_id = manga.folder_ids[0]
        folder = review.folder_payload(folder_id, Activity.EDIT)["folder"]
        self.assertEqual(
            [(image["name"], image["selected"]) for image in folder["images"]],
            [("1.jpg", True), ("10.jpg", True)],
        )

        first_id, tenth_id = (image["id"] for image in folder["images"])
        review.set_position(Activity.EDIT, folder_id, tenth_id)
        review.set_selection(Activity.EDIT, folder_id, tenth_id, False)

        self.assertEqual(
            json.loads(editing.read_text()),
            {
                "last_folder": "Part III - Ch 21",
                "last_image": "10.jpg",
                "folders": {
                    "Old Entry": {"selected_images": ["old.jpg"]},
                    "Part III - Ch 21": {"selected_images": ["1.jpg", "2.PNG"]},
                },
            },
        )

    def test_read_payload_has_no_paths_or_selection_semantics(self) -> None:
        manga = self.build_manga()
        payload = ReviewService(manga).folder_payload(manga.folder_ids[0], Activity.READ)
        serialized = json.dumps(payload)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("selected", serialized)
        self.assertNotIn("exports", serialized)

    def test_read_selection_is_never_persisted(self) -> None:
        manga = self.build_manga()
        folder_id = manga.folder_ids[0]
        image_id = manga.folder(folder_id).image_ids[0]
        review = ReviewService(manga)
        with patch.object(EditingStore, "set_selection") as save:
            with self.assertRaises(WrongActivityError):
                review.set_selection(Activity.READ, folder_id, image_id, True)
        save.assert_not_called()


class ServiceTests(ServerFixture):
    PAGE = "page"

    def setUp(self) -> None:
        super().setUp()
        self.service = LibraryService(self.scan_result)
        self.grant = self.service.claim_controller(self.PAGE)
        self.token = self.grant["token"]

    def manga_id(self, name: str = "Series One", page: str = PAGE, token: str = "") -> str:
        library = self.service.library(page, token or self.token)
        return next(manga["id"] for manga in library["mangas"] if manga["name"] == name)

    def open_edit(self) -> tuple[str, str]:
        manga_id = self.manga_id()
        self.service.scan_manga(self.PAGE, self.token, manga_id)
        manga = self.service.open_manga(self.PAGE, self.token, manga_id, Activity.EDIT)["manga"]
        folder_id = manga["folders"][0]["id"]
        folder = self.service.folder(self.PAGE, self.token, folder_id, Activity.EDIT)["folder"]
        return folder_id, folder["images"][0]["id"]

    def test_library_lists_names_without_scanning_manga_contents(self) -> None:
        (self.root / "Empty Manga").mkdir()
        with patch(
            "pocket_manga_editor.service.scan_manga",
            side_effect=AssertionError("library scan must not open manga"),
        ):
            library = self.service.rescan(self.PAGE, self.token)
        self.assertEqual(
            library["mangas"],
            [{"id": manga["id"], "name": manga["name"]} for manga in library["mangas"]],
        )
        self.assertEqual(
            [manga["name"] for manga in library["mangas"]],
            ["Empty Manga", "Series One", "Series Two"],
        )

    def test_opening_a_manga_reports_why_it_cannot_be_read(self) -> None:
        (self.root / "No Entries").mkdir()
        (self.root / "No Entries" / "loose.jpg").write_bytes(b"loose")
        (self.root / "No Images" / "Chapter 1").mkdir(parents=True)
        self.service.rescan(self.PAGE, self.token)
        cases = {
            "No Entries": "no_entry_folders",
            "No Images": "no_images",
            "Series Two": "manga_missing",
        }
        (self.root / "Series Two" / "Bonus scans" / "scan-a.png").unlink()
        (self.root / "Series Two" / "Bonus scans").rmdir()
        (self.root / "Series Two").rmdir()
        for name, code in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(MangaUnavailableError) as raised:
                    self.service.scan_manga(self.PAGE, self.token, self.manga_id(name))
                self.assertEqual(raised.exception.code, code)

    def test_reopening_a_manga_rescans_it_and_retires_old_ids(self) -> None:
        old_folder_id, _image_id = self.open_edit()
        self.add_image("Series One", "Volume Three - Ch 30", "1.jpg", b"new")
        manga_id = self.manga_id()

        self.service.scan_manga(self.PAGE, self.token, manga_id)
        manga = self.service.open_manga(self.PAGE, self.token, manga_id, Activity.EDIT)["manga"]

        self.assertIn("Volume Three - Ch 30", [folder["name"] for folder in manga["folders"]])
        with self.assertRaises(SnapshotLookupError):
            self.service.folder(self.PAGE, self.token, old_folder_id, Activity.EDIT)

    def test_choosing_an_activity_reuses_the_scan_from_the_tile_tap(self) -> None:
        manga_id = self.manga_id()
        self.service.scan_manga(self.PAGE, self.token, manga_id)
        with patch(
            "pocket_manga_editor.service.scan_manga",
            side_effect=AssertionError("Read/Edit must reuse the tile scan"),
        ):
            self.service.open_manga(self.PAGE, self.token, manga_id, Activity.READ)
            self.service.open_manga(self.PAGE, self.token, manga_id, Activity.EDIT)

    def test_always_active_service_persists_review_and_keeps_controller_on_error(self) -> None:
        snapshot_id = self.service.status().snapshot_id
        self.assertEqual(self.service.library(self.PAGE, self.token)["snapshot_id"], snapshot_id)
        folder_id, image_id = self.open_edit()
        with patch.object(EditingStore, "set_selection", side_effect=OSError("disk")):
            with self.assertRaises(ReviewSaveError):
                self.service.set_selection(
                    self.PAGE,
                    self.token,
                    Activity.EDIT,
                    folder_id,
                    image_id,
                    True,
                )
        self.assertTrue(self.service.status().active_controller)
        self.assertEqual(self.service.status().snapshot_id, snapshot_id)

    def test_rescan_swaps_only_a_complete_library_and_retains_controller(self) -> None:
        original = self.service.status().snapshot_id
        self.add_image("Series Three", "Chapter 1", "new.jpg", b"new")
        with (
            patch(
                "pocket_manga_editor.service.LibraryIndex.build",
                side_effect=SnapshotError("bad snapshot"),
            ),
            patch("pocket_manga_editor.service.LOGGER.exception"),
        ):
            with self.assertRaises(RescanError):
                self.service.rescan(self.PAGE, self.token)
        self.assertEqual(self.service.status().snapshot_id, original)
        self.assertEqual(len(self.service.library(self.PAGE, self.token)["mangas"]), 2)

        library = self.service.rescan(self.PAGE, self.token)
        self.assertNotEqual(library["snapshot_id"], original)
        self.assertEqual(len(library["mangas"]), 3)
        self.assertTrue(self.service.status().active_controller)

    def test_takeover_waits_for_an_authorized_durable_operation(self) -> None:
        folder_id, image_id = self.open_edit()
        started = threading.Event()
        finish = threading.Event()
        takeover_done = threading.Event()
        original_save = EditingStore.set_selection

        def blocked_save(store, *args, **kwargs):
            started.set()
            self.assertTrue(finish.wait(2))
            return original_save(store, *args, **kwargs)

        failures: list[BaseException] = []

        def save_selection() -> None:
            try:
                self.service.set_selection(
                    self.PAGE,
                    self.token,
                    Activity.EDIT,
                    folder_id,
                    image_id,
                    True,
                )
            except BaseException as exc:
                failures.append(exc)

        replacement: list[str] = []

        def takeover() -> None:
            try:
                replacement.append(
                    self.service.claim_controller("other-page", takeover=True)["token"]
                )
            except BaseException as exc:
                failures.append(exc)
            finally:
                takeover_done.set()

        with patch.object(
            EditingStore,
            "set_selection",
            autospec=True,
            side_effect=blocked_save,
        ):
            thread = threading.Thread(target=save_selection)
            thread.start()
            self.assertTrue(started.wait(1))
            takeover_thread = threading.Thread(target=takeover)
            takeover_thread.start()
            self.assertFalse(takeover_done.wait(0.05))
            finish.set()
            thread.join(2)
            takeover_thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(takeover_thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(replacement), 1)
        with self.assertRaises(ControllerTransferredError):
            self.service.library(self.PAGE, self.token)
        self.assertEqual(
            len(self.service.library("other-page", replacement[0])["mangas"]),
            2,
        )
        selected = EditingStore(self.root).load(scan_manga(self.root, "Series One").manga)
        self.assertEqual(selected.folders["Part III - Ch 21"], ("1.jpg",))


class APITests(ServerFixture):
    HOST = "desktop.local:8787"
    ORIGIN = "http://desktop.local:8787"
    PAGE = "page-instance-one"

    def setUp(self) -> None:
        super().setUp()
        self.service = LibraryService(self.scan_result)
        self.api = API(self.service, allowed_hosts={"desktop.local"})
        self.controller_page = self.PAGE
        self.controller_token = "x" * 43

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
            headers["X-Page-Id"] = self.controller_page
            headers["X-Controller-Token"] = self.controller_token
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

    def claim(self, page: str | None = None, *, takeover: bool = False) -> APIResponse:
        page_id = page or self.PAGE
        response = self.json_request(
            "POST",
            "/api/controller/takeover" if takeover else "/api/controller/claim",
            {"page_id": page_id},
            controller=False,
        )
        if response.status == 200:
            controller = self.payload(response)["controller"]
            self.controller_page = str(controller["page_id"])
            self.controller_token = str(controller["token"])
        return response

    def get(self, target: str) -> APIResponse:
        return self.api.handle(
            "GET",
            target,
            self.headers(),
            client_address="192.168.1.25",
        )

    def open_activity(self, activity: str = "read") -> dict[str, object]:
        manga_id = self.payload(self.get("/api/library"))["mangas"][0]["id"]
        self.assertEqual(self.json_request("POST", f"/api/manga/{manga_id}/scan", {}).status, 200)
        response = self.get(f"/api/manga/{manga_id}?activity={activity}")
        self.assertEqual(response.status, 200)
        return self.payload(response)

    def test_status_and_claim_are_open_but_every_library_route_needs_controller(self) -> None:
        status = self.api.handle(
            "GET",
            "/api/status",
            {"Host": self.HOST},
            client_address="127.0.0.1",
        )
        self.assertEqual(status.status, 200)
        self.assertEqual(self.payload(status)["status"], {"server": "available"})
        ready = self.api.handle("GET", "/health", {"Host": self.HOST}, client_address="127.0.0.1")
        self.assertEqual(ready.status, 200)
        self.assertEqual(self.payload(ready), make_health())
        unclaimed = self.get("/api/library")
        self.assertEqual(unclaimed.status, 409)
        self.assertEqual(self.payload(unclaimed)["error"]["code"], "controller_required")
        self.assertEqual(self.claim().status, 200)
        self.assertEqual(self.get("/api/library").status, 200)
        self.assertEqual(
            self.json_request("POST", "/api/pair", {}, controller=False).status,
            404,
        )

    def test_claim_takeover_release_and_exact_bodies(self) -> None:
        malformed = self.json_request(
            "POST",
            "/api/controller/claim",
            {"page_id": self.PAGE, "extra": True},
            controller=False,
        )
        self.assertEqual(malformed.status, 400)
        claimed = self.claim()
        self.assertEqual(claimed.status, 200)
        self.assertEqual(self.payload(claimed)["controller"]["page_id"], self.PAGE)
        first_token = self.controller_token
        blocked = self.claim("second-page")
        self.assertEqual(blocked.status, 423)
        self.assertEqual(self.payload(blocked)["error"]["code"], "controller_occupied")
        heartbeat = self.json_request(
            "POST",
            "/api/controller/heartbeat",
            {"page_id": self.PAGE},
            controller=False,
        )
        self.assertEqual(heartbeat.status, 404)

        taken = self.claim("second-page", takeover=True)
        self.assertEqual(taken.status, 200)
        second_token = self.controller_token
        self.controller_page = self.PAGE
        self.controller_token = first_token
        stale = self.get("/api/library")
        self.assertEqual(stale.status, 423)
        self.assertEqual(self.payload(stale)["error"]["code"], "controller_transferred")
        self.controller_page = "second-page"
        self.controller_token = second_token
        self.assertEqual(self.get("/api/library").status, 200)
        released = self.json_request(
            "POST",
            "/api/controller/release",
            {"page_id": "second-page", "token": second_token},
            controller=False,
        )
        self.assertEqual(released.status, 200)
        self.assertEqual(self.get("/api/library").status, 409)

    def test_protected_routes_require_both_controller_headers(self) -> None:
        self.assertEqual(self.claim().status, 200)
        response = self.api.handle(
            "GET",
            "/api/library",
            {"Host": self.HOST, "X-Page-Id": self.PAGE},
            client_address="192.168.1.25",
        )
        self.assertEqual(response.status, 400)

    def test_activity_queries_and_patch_writes_are_strict_and_independent(self) -> None:
        self.assertEqual(self.claim().status, 200)
        library = self.payload(self.get("/api/library"))
        self.assertEqual(set(library["mangas"][0]), {"id", "name"})
        manga_id = library["mangas"][0]["id"]
        for query in ("", "?activity=", "?activity=read&activity=edit", "?mode=read"):
            with self.subTest(query=query):
                self.assertEqual(self.get(f"/api/manga/{manga_id}{query}").status, 400)

        read_manga = self.payload(self.get(f"/api/manga/{manga_id}?activity=read"))["manga"]
        folder_id = read_manga["folders"][0]["id"]
        read_folder = self.payload(self.get(f"/api/folder/{folder_id}?activity=read"))["folder"]
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
        edit_folder = self.payload(self.get(f"/api/folder/{folder_id}?activity=edit"))["folder"]
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
        manga_ref = scan_manga(self.root, "Series One").manga
        self.assertEqual(ReadingStore(self.root).load(manga_ref).last_image, "2.PNG")
        editing = EditingStore(self.root).load(manga_ref)
        self.assertEqual(editing.position.last_image, "10.jpg")
        self.assertEqual(editing.folders["Part III - Ch 21"], ("1.jpg",))
        self.assertEqual(
            self.payload(selected)["selection"],
            {"folder_id": folder_id, "image_id": first_id, "selected": True},
        )

    def test_activity_rebind_rejects_stale_writes(self) -> None:
        self.assertEqual(self.claim().status, 200)
        read = self.open_activity("read")["manga"]
        folder_id = read["folders"][0]["id"]
        image_id = self.payload(self.get(f"/api/folder/{folder_id}?activity=read"))["folder"][
            "images"
        ][0]["id"]
        manga_id = read["id"]
        self.get(f"/api/manga/{manga_id}?activity=edit")
        stale = self.json_request(
            "PATCH",
            f"/api/read/folder/{folder_id}/position",
            {"image_id": image_id},
        )
        self.assertEqual(stale.status, 409)
        self.assertEqual(self.payload(stale)["error"]["code"], "wrong_activity")

    def test_images_are_controller_protected_validated_and_privately_cacheable(self) -> None:
        self.assertEqual(self.claim().status, 200)
        manga = self.open_activity("read")["manga"]
        folder_id = manga["folders"][0]["id"]
        folder = self.payload(self.get(f"/api/folder/{folder_id}?activity=read"))["folder"]
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
        missing_host = self.api.handle("GET", "/api/status", {}, client_address="127.0.0.1")
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
            json.dumps({"page_id": self.PAGE}).encode(),
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
            json.dumps({"page_id": self.PAGE}).encode(),
            client_address="192.168.1.25",
        )
        self.assertEqual(cross_origin.status, 403)
        self.assertEqual(wrong_type.status, 415)

    def test_rescan_returns_new_library_keeps_controller_and_retains_old_on_failure(self) -> None:
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
                "pocket_manga_editor.service.scan_library",
                side_effect=ScanError("broken source"),
            ),
            patch("pocket_manga_editor.service.LOGGER.exception"),
        ):
            failed = self.json_request("POST", "/api/library/rescan", {})
        self.assertEqual(failed.status, 503)
        self.assertEqual(self.payload(failed)["error"]["code"], "rescan_failed")
        current = self.payload(self.get("/api/library"))
        self.assertEqual(current["snapshot_id"], stable)
        extra = self.json_request("POST", "/api/library/rescan", {"unexpected": True})
        self.assertEqual(extra.status, 400)

    def test_manga_scan_route_is_exact_and_reports_unreadable_manga(self) -> None:
        (self.root / "Loose Pages").mkdir()
        (self.root / "Loose Pages" / "1.jpg").write_bytes(b"loose")
        self.assertEqual(self.claim().status, 200)
        self.assertEqual(self.json_request("POST", "/api/library/rescan", {}).status, 200)
        mangas = {
            manga["name"]: manga["id"] for manga in self.payload(self.get("/api/library"))["mangas"]
        }

        scanned = self.json_request("POST", f"/api/manga/{mangas['Series One']}/scan", {})
        self.assertEqual(scanned.status, 200)
        self.assertEqual(
            self.payload(scanned)["manga"], {"id": mangas["Series One"], "name": "Series One"}
        )
        loose = self.json_request("POST", f"/api/manga/{mangas['Loose Pages']}/scan", {})
        self.assertEqual(loose.status, 409)
        self.assertEqual(self.payload(loose)["error"]["code"], "no_entry_folders")
        stale = self.json_request("POST", "/api/manga/m_unknown/scan", {})
        self.assertEqual(stale.status, 404)
        self.assertEqual(self.payload(stale)["error"]["code"], "stale_snapshot")
        extra = self.json_request(
            "POST", f"/api/manga/{mangas['Series One']}/scan", {"unexpected": True}
        )
        self.assertEqual(extra.status, 400)
        self.assertEqual(self.get(f"/api/manga/{mangas['Series One']}/scan").status, 404)

    def test_old_destructive_and_pairing_routes_are_absent(self) -> None:
        self.assertEqual(self.claim().status, 200)
        for method, route in (
            ("GET", "/api/volume/v_old"),
            ("GET", "/api/page/p_old/image"),
            ("POST", "/api/export"),
            ("GET", "/api/manga/m_any/export-preview"),
            ("POST", "/api/manga/m_any/export"),
            ("POST", "/api/complete"),
            ("POST", "/api/pair"),
        ):
            with self.subTest(route=route):
                response = (
                    self.json_request(method, route, {}) if method == "POST" else self.get(route)
                )
                self.assertEqual(response.status, 404)


class HTTPServiceTests(ServerFixture):
    def setUp(self) -> None:
        super().setUp()
        self.services: list[HTTPService] = []

    def tearDown(self) -> None:
        for service in self.services:
            service.stop()
        super().tearDown()

    def service(self, port: int) -> HTTPService:
        service = HTTPService(
            LibraryService(self.scan_result),
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
            "pocket_manga_editor.server._HTTPServer",
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
            self.assertIn(b"/static/app.js?v=lazy-scan-v5", shell)
            self.assertIn(b"/static/styles.css?v=lazy-scan-v5", shell)
            # Idle connections are never reused, so they cannot race a close.
            self.assertEqual(response.getheader("Connection"), "close")
            self.assertIn(
                "default-src 'self'",
                response.getheader("Content-Security-Policy") or "",
            )
            connection.request("GET", "/health")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), make_health())
            connection.request("GET", "/static/app.js")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b"X-Page-Id", response.read())

            def controller_request(route, page, token=None):
                body = {"page_id": page}
                if token is not None:
                    body["token"] = token
                connection.request(
                    "POST",
                    f"/api/controller/{route}",
                    json.dumps(body),
                    {"Content-Type": "application/json"},
                )
                reply = connection.getresponse()
                return reply.status, json.loads(reply.read())

            status, first = controller_request("claim", "first-page")
            self.assertEqual(status, 200)
            first_token = first["controller"]["token"]
            status, occupied = controller_request("claim", "second-page")
            self.assertEqual(status, 423)
            self.assertEqual(occupied["error"]["code"], "controller_occupied")
            status, second = controller_request("takeover", "second-page")
            self.assertEqual(status, 200)
            second_token = second["controller"]["token"]
            connection.request(
                "GET",
                "/api/library",
                headers={"X-Page-Id": "first-page", "X-Controller-Token": first_token},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 423)
            self.assertEqual(json.loads(response.read())["error"]["code"], "controller_transferred")
            connection.request(
                "GET",
                "/api/library",
                headers={"X-Page-Id": "second-page", "X-Controller-Token": second_token},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(len(json.loads(response.read())["mangas"]), 2)
            status, released = controller_request("release", "second-page", second_token)
            self.assertEqual(status, 200)
            self.assertTrue(released["released"])
            status, reclaimed = controller_request("claim", "first-page")
            self.assertEqual(status, 200)
            self.assertNotEqual(reclaimed["controller"]["token"], first_token)
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

    def test_port_conflict_preserves_the_ready_service(self) -> None:
        service = self.service(48124)
        snapshot_id = service.service.status().snapshot_id
        with patch(
            "pocket_manga_editor.server._HTTPServer",
            side_effect=OSError("Address already in use"),
        ):
            status = service.start()
        self.assertFalse(status.running)
        self.assertIn("Could not listen", status.error or "")
        self.assertEqual(service.service.status().snapshot_id, snapshot_id)


if __name__ == "__main__":
    unittest.main()
