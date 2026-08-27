"""Headless integration tests for the filesystem-faithful desktop interface."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QByteArray, QEvent, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QCloseEvent, QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from pocket_manga_editor.companion import (  # noqa: E402
    CompanionActivity,
    CompanionCoordinator,
    CompanionState,
    CredentialVerifierStore,
)
from pocket_manga_editor.exporter import (  # noqa: E402
    MangaExportResult,
    export_manga,
    exported_image_name,
)
from pocket_manga_editor.library_lock import LibraryBusyError  # noqa: E402
from pocket_manga_editor.main_window import MainWindow  # noqa: E402
from pocket_manga_editor.storage import EditingStore, ReadingStore  # noqa: E402


class FakeCompanionServer:
    def __init__(self) -> None:
        self.running = True
        self.error = None
        self.url = "http://192.168.1.20:8765/"
        self.port = 8765
        self.public_host = "192.168.1.20"

    def status(self) -> SimpleNamespace:
        return SimpleNamespace(
            running=self.running,
            host="0.0.0.0",
            port=self.port,
            public_host=self.public_host,
            url=self.url,
            error=self.error,
            lan_address_available=True,
        )

    def start(self) -> SimpleNamespace:
        self.running = True
        self.error = None
        return self.status()

    def restart(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        public_host: str | None = None,
    ) -> SimpleNamespace:
        del host
        if port is not None:
            self.port = port
        if public_host is not None:
            self.public_host = public_host or "192.168.1.20"
        self.url = f"http://{self.public_host}:{self.port}/"
        return self.start()


class FilesystemLayoutTests(unittest.TestCase):
    """Exercise layout, persistence, export, completion, and handoff."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setOrganizationName("Pocket Manga Editor UI Tests")
        cls.app.setApplicationName("Pocket Manga Editor UI Tests")
        cls.app.setStyle("Fusion")
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        QSettings.setPath(
            QSettings.Format.IniFormat,
            QSettings.Scope.UserScope,
            self.temporary.name,
        )
        self.first_folder_path = self.root / "Example Manga" / "Arc 2 - Opening"
        self.second_folder_path = self.root / "Example Manga" / "Arc 10 - Finale"
        self.first_folder_path.mkdir(parents=True)
        self.second_folder_path.mkdir(parents=True)
        self._write_image(self.first_folder_path / "scan 2.png")
        self._write_image(self.first_folder_path / "scan 10.png")
        self._write_image(self.second_folder_path / "final page.png")

        settings = QSettings()
        settings.clear()
        settings.setValue("library/working_directory", str(self.root))
        settings.sync()
        self.window = MainWindow()

    def tearDown(self) -> None:
        self.window.close()
        self.window.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temporary.cleanup()

    def _write_image(self, path: Path) -> None:
        image = QImage(600, 1000, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.white)
        self.assertTrue(image.save(str(path)))

    def _show_window(self, width: int = 1280, height: int = 800) -> None:
        self.window.resize(width, height)
        self.window.show()
        QTest.qWait(75)
        self.app.processEvents()

    def _replace_window_with_companion(
        self,
    ) -> tuple[CompanionCoordinator, FakeCompanionServer]:
        self.window.close()
        self.window.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        coordinator = CompanionCoordinator()
        server = FakeCompanionServer()
        self.window = MainWindow(
            companion_coordinator=coordinator,
            companion_server=server,  # type: ignore[arg-type]
        )
        return coordinator, server

    def _pair_controller(
        self, coordinator: CompanionCoordinator
    ) -> tuple[str, str, str]:
        code = self.window._pairing_code
        self.assertIsNotNone(code)
        credential = coordinator.pair(code or "")
        client_id = "iphone-home-screen"
        page_instance_id = "document-instance-1"
        coordinator.claim_controller(credential, client_id, page_instance_id)
        return credential, client_id, page_instance_id

    def test_scanned_names_and_natural_order_are_shown_exactly(self) -> None:
        manga = self.window.scan_result.mangas[0]
        self.assertEqual(manga.name, "Example Manga")
        self.assertEqual(
            [folder.name for folder in manga.folders],
            ["Arc 2 - Opening", "Arc 10 - Finale"],
        )
        self.assertEqual(
            [image.name for image in manga.folders[0].images],
            ["scan 2.png", "scan 10.png"],
        )
        self.assertEqual(
            [
                self.window.folder_combo.itemText(index)
                for index in range(self.window.folder_combo.count())
            ],
            ["Arc 2 - Opening", "Arc 10 - Finale"],
        )
        self.assertEqual(
            self.window.heading_label.text(), "Example Manga  ·  Arc 2 - Opening"
        )
        self.assertEqual(self.window.image_name_label.text(), "scan 2.png")

    def test_manga_open_resumes_latest_pair_but_folder_picker_starts_first(self) -> None:
        manga = self.window.current_manga
        store = self.window.editing_store
        assert manga is not None and store is not None
        first, _second = manga.folders
        store.set_position(manga, first.name, "scan 10.png")
        self.window.current_manga = None
        self.window.current_folder = None

        self.window._populate_folders(manga)
        self.assertEqual(self.window.current_folder, first)
        self.assertEqual(self.window.image_name_label.text(), "scan 10.png")

        self.window.folder_combo.setCurrentIndex(1)
        self.window.folder_combo.setCurrentIndex(0)

        self.assertEqual(self.window.current_folder, first)
        self.assertEqual(self.window.image_name_label.text(), "scan 2.png")

    def test_canvas_dominates_split_and_sidebar_scrolls(self) -> None:
        self._show_window()
        left_width, right_width = self.window.main_splitter.sizes()
        ratio = left_width / (left_width + right_width)
        self.assertGreaterEqual(ratio, 0.62)
        self.assertLessEqual(ratio, 0.75)
        self.assertTrue(self.window.sidebar_scroll.widgetResizable())
        self.assertEqual(
            self.window.sidebar_scroll.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        self.window.resize(820, 620)
        QTest.qWait(50)
        self.assertGreater(self.window.sidebar_scroll.verticalScrollBar().maximum(), 0)
        self.assertGreater(min(self.window.main_splitter.sizes()), 0)

    def test_review_shortcuts_select_and_navigate_images(self) -> None:
        self._show_window()
        QTest.keyClick(self.window.canvas, Qt.Key.Key_Space)
        self.assertEqual(self.window.selected_images, {"scan 2.png"})
        self.assertTrue(self.window.canvas.property("selected"))
        self.assertTrue(self.window.canvas.badge.isVisible())
        QTest.keyClick(self.window.canvas, Qt.Key.Key_Right)
        self.assertEqual(self.window.current_image_index, 1)
        self.assertEqual(self.window.progress_label.text(), "2 / 2")
        self.assertEqual(self.window.image_name_label.text(), "scan 10.png")

    def test_splitter_position_is_restored(self) -> None:
        self._show_window()
        self.window.main_splitter.setSizes([760, 480])
        self.app.processEvents()
        first_ratio = self.window.main_splitter.sizes()[0] / sum(
            self.window.main_splitter.sizes()
        )
        self.window.close()
        self.window.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.window = MainWindow()
        self._show_window()
        restored_ratio = self.window.main_splitter.sizes()[0] / sum(
            self.window.main_splitter.sizes()
        )
        self.assertAlmostEqual(first_ratio, restored_ratio, delta=0.03)

    def test_invalid_splitter_state_falls_back_safely(self) -> None:
        self.window.close()
        self.window.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        settings = QSettings()
        settings.setValue("window/main_splitter", QByteArray(b"invalid"))
        settings.sync()
        self.window = MainWindow()
        self._show_window()
        sizes = self.window.main_splitter.sizes()
        self.assertGreater(min(sizes), 0)
        self.assertGreater(sizes[0], sizes[1])

    def test_linked_working_directory_is_rejected_before_canonicalization(self) -> None:
        target = self.root / "alternate-library"
        target.mkdir()
        alias = self.root / "linked-library"
        try:
            alias.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory links are unavailable: {exc}")
        original = self.window.working_directory

        with patch.object(QMessageBox, "critical") as critical:
            self.window._set_working_directory(alias)

        self.assertEqual(self.window.working_directory, original)
        self.assertEqual(critical.call_count, 1)
        self.assertFalse((target / ".pocket-manga-editor").exists())

    def test_unsafe_output_disables_output_actions_not_editing(self) -> None:
        manga = self.window.current_manga
        folder = self.window.current_folder
        assert manga is not None and folder is not None
        workspace = self.root / ".pocket-manga-editor" / manga.name
        workspace.mkdir(parents=True, exist_ok=True)
        target = self.root / "unsafe-output-target"
        target.mkdir()
        try:
            (workspace / "output").symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory links are unavailable: {exc}")

        self.window._load_folder(folder, snapshot=self.window.editing_snapshot)

        self.assertEqual(self.window.current_folder, folder)
        self.assertTrue(self.window.toggle_button.isEnabled())
        self.assertFalse(self.window.open_output_button.isEnabled())

        self.window.selected_images = {folder.images[0].name}
        with (
            patch.object(QMessageBox, "critical") as critical,
            patch.object(QMessageBox, "question") as question,
        ):
            self.window.export_selection()
        self.assertEqual(critical.call_count, 1)
        self.assertEqual(question.call_count, 0)
        self.assertEqual(self.window.current_folder, folder)

    def test_busy_save_keeps_outgoing_folder_snapshot_when_switching(self) -> None:
        manga = self.window.current_manga
        first = self.window.current_folder
        store = self.window.editing_store
        assert manga is not None and first is not None and store is not None
        second = manga.folders[1]
        self.window.current_image_index = 1
        self.window.selected_images = {"scan 10.png"}
        with patch.object(
            store,
            "save_folder",
            side_effect=LibraryBusyError("Another mutation is in progress."),
        ):
            self.window._load_folder(second)
        self.assertEqual(self.window.current_folder, second)
        self.assertTrue(self.window._pending_session_saves)
        self.window._session_save_timer.stop()
        self.assertTrue(self.window._flush_pending_session_saves())
        restored = store.load(manga)
        self.assertEqual(restored.last_folder, first.name)
        self.assertEqual(restored.last_image, "scan 10.png")
        self.assertEqual(
            restored.folders[first.name].selected_images,
            frozenset({"scan 10.png"}),
        )

    def test_busy_save_defers_close_until_snapshot_is_persisted(self) -> None:
        self._show_window()
        manga = self.window.current_manga
        folder = self.window.current_folder
        store = self.window.editing_store
        assert manga is not None and folder is not None and store is not None
        self.window.current_image_index = 1
        self.window.selected_images = {"scan 10.png"}
        event = QCloseEvent()
        with patch.object(
            store,
            "save_folder",
            side_effect=LibraryBusyError("Another mutation is in progress."),
        ):
            self.window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertTrue(self.window._close_requested)
        self.assertTrue(self.window._pending_session_saves)
        self.window._session_save_timer.stop()
        self.assertTrue(self.window._flush_pending_session_saves())
        restored = store.load(manga)
        self.assertEqual(restored.last_folder, folder.name)
        self.assertEqual(restored.last_image, "scan 10.png")
        self.assertEqual(
            restored.folders[folder.name].selected_images,
            frozenset({"scan 10.png"}),
        )
        self.app.processEvents()
        self.assertFalse(self.window.isVisible())

    def test_one_export_synchronizes_selections_from_multiple_folders(self) -> None:
        manga = self.window.current_manga
        assert manga is not None
        first, second = manga.folders
        self.window.selected_images = {first.images[0].name}
        self.assertTrue(self.window._save_session())
        self.window._load_folder(second)
        self.window.selected_images = {second.images[0].name}
        self.assertTrue(self.window._save_session())
        with (
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(QMessageBox, "information"),
        ):
            self.window.export_selection()
        output = self.root / ".pocket-manga-editor" / manga.name / "output"
        self.assertTrue(
            (output / first.name / exported_image_name(first.name, first.images[0].name)).is_file()
        )
        self.assertTrue(
            (output / second.name / exported_image_name(second.name, second.images[0].name)).is_file()
        )

    def test_zero_selection_cleanup_disables_missing_output_button(self) -> None:
        manga = self.window.current_manga
        folder = self.window.current_folder
        store = self.window.editing_store
        assert manga is not None and folder is not None and store is not None
        image = folder.images[0]
        store.set_selection(manga, folder.name, image.name, True)
        first = export_manga(self.root, manga)
        self.assertTrue(first.output_directory.is_dir())
        store.set_selection(manga, folder.name, image.name, False)
        self.window._populate_folders(manga)

        with (
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(QMessageBox, "information"),
        ):
            self.window.export_selection()

        self.assertFalse(first.output_directory.exists())
        self.assertFalse(self.window.open_output_button.isEnabled())

    def test_committed_export_warning_detaches_before_follow_up_recovery(self) -> None:
        manga = self.window.current_manga
        folder = self.window.current_folder
        assert manga is not None and folder is not None
        self.window.selected_images = {folder.images[0].name}
        self.assertTrue(self.window._save_session())
        result = MangaExportResult(
            self.root / ".pocket-manga-editor" / manga.name / "output",
            (),
            1,
            0,
            0,
            ("Temporary export cleanup was interrupted.",),
        )
        observed: dict[str, object] = {}

        def detached_rescan(**_kwargs):
            observed["manga"] = self.window.current_manga
            observed["folder"] = self.window.current_folder
            observed["pending"] = bool(self.window._pending_session_saves)
            return True, None

        with (
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(QMessageBox, "warning"),
            patch("pocket_manga_editor.main_window.export_manga", return_value=result),
            patch.object(self.window, "_rescan", side_effect=detached_rescan),
        ):
            self.window.export_selection()

        self.assertIsNone(observed["manga"])
        self.assertIsNone(observed["folder"])
        self.assertFalse(observed["pending"])

    def test_rescan_recovers_transactions_before_any_session_save(self) -> None:
        events: list[str] = []
        original_save = self.window._save_session
        original_scan = self.window.scan_result

        export_recovery = SimpleNamespace(
            committed_count=1,
            rolled_back_count=0,
            discarded_count=0,
            recovered_count=1,
            warnings=(),
        )
        completion_recovery = SimpleNamespace(
            rolled_back_count=0,
            cleaned_count=0,
            recovered_count=0,
            warnings=(),
        )

        def recover_export(_root):
            events.append("export recovery")
            return export_recovery

        def recover_completion(_root):
            events.append("completion recovery")
            return completion_recovery

        def scan(_root):
            events.append("scan")
            return original_scan

        def save():
            events.append("save")
            return original_save()

        with (
            patch(
                "pocket_manga_editor.main_window.recover_interrupted_exports",
                side_effect=recover_export,
            ),
            patch(
                "pocket_manga_editor.main_window.recover_interrupted_completions",
                side_effect=recover_completion,
            ),
            patch(
                "pocket_manga_editor.main_window.scan_working_directory",
                side_effect=scan,
            ),
            patch.object(self.window, "_save_session", side_effect=save),
        ):
            rescanned, _recovery = self.window._rescan(show_recovery_dialog=False)

        self.assertTrue(rescanned)
        self.assertEqual(events[:3], ["export recovery", "completion recovery", "scan"])
        self.assertIn("save", events[3:])

    def test_completion_moves_output_to_first_batch_and_detaches_source(self) -> None:
        manga = self.window.current_manga
        folder = self.window.current_folder
        store = self.window.editing_store
        assert manga is not None and folder is not None and store is not None
        image = folder.images[0]
        store.set_selection(manga, folder.name, image.name, True)
        export_manga(self.root, manga)
        self.window._populate_folders(manga)
        self.window._session_save_timer.start(1000)
        with (
            patch.object(
                QMessageBox,
                "warning",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(QMessageBox, "information"),
        ):
            self.window.complete_current_manga()
        workspace = self.root / ".pocket-manga-editor" / manga.name
        completed = workspace / "completed" / "batch-0001"
        self.assertFalse(manga.path.exists())
        self.assertTrue(
            (completed / folder.name / exported_image_name(folder.name, image.name)).is_file()
        )
        self.assertFalse((workspace / "reading.json").exists())
        self.assertFalse((workspace / "editing.json").exists())
        self.assertIsNone(self.window.current_manga)
        self.assertFalse(self.window._pending_session_saves)
        self.window.close()
        self.app.processEvents()
        self.assertFalse((workspace / "editing.json").exists())

    def test_completion_cancel_preserves_source_output_and_state(self) -> None:
        manga = self.window.current_manga
        folder = self.window.current_folder
        store = self.window.editing_store
        assert manga is not None and folder is not None and store is not None
        store.set_selection(manga, folder.name, folder.images[0].name, True)
        result = export_manga(self.root, manga)
        self.window._populate_folders(manga)
        with patch.object(
            QMessageBox,
            "warning",
            return_value=QMessageBox.StandardButton.Cancel,
        ) as warning:
            self.window.complete_current_manga()
        self.assertEqual(warning.call_count, 1)
        self.assertTrue(manga.path.is_dir())
        self.assertTrue(result.output_directory.is_dir())
        self.assertFalse(
            (self.root / ".pocket-manga-editor" / manga.name / "completed").exists()
        )

    def test_remembered_phone_enters_companion_mode_at_startup(self) -> None:
        self.window.close()
        self.window.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

        credential_store = CredentialVerifierStore(
            self.root / ".test-companion-device.json"
        )
        pairing_coordinator = CompanionCoordinator(
            credential_store=credential_store
        )
        pairing = pairing_coordinator.start_pairing()
        pairing_coordinator.pair(pairing.code)
        coordinator = CompanionCoordinator(credential_store=credential_store)
        server = FakeCompanionServer()
        with patch.object(QMessageBox, "question") as question:
            self.window = MainWindow(
                companion_coordinator=coordinator,
                companion_server=server,  # type: ignore[arg-type]
            )
            self.app.processEvents()

        self.assertEqual(
            coordinator.status().state, CompanionState.COMPANION_ACTIVE
        )
        self.assertTrue(self.window._companion_ui_active)
        self.assertIs(
            self.window.viewer_stack.currentWidget(),
            self.window.companion_status_panel,
        )
        self.assertFalse(self.window.folder_combo.isEnabled())
        self.assertIn("started automatically", self.window.statusBar().currentMessage())
        question.assert_not_called()

    def test_unpaired_phone_keeps_desktop_mode_at_startup(self) -> None:
        coordinator, _server = self._replace_window_with_companion()
        self.app.processEvents()

        self.assertEqual(
            coordinator.status().state, CompanionState.DESKTOP_ACTIVE
        )
        self.assertFalse(self.window._companion_ui_active)
        self.assertTrue(self.window.folder_combo.isEnabled())

    def test_companion_edit_reloads_shared_position_and_selection(self) -> None:
        coordinator, _server = self._replace_window_with_companion()
        with (
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(QMessageBox, "information"),
        ):
            self.window.start_companion_mode()
        self.assertEqual(coordinator.status().state, CompanionState.COMPANION_ACTIVE)
        self.assertFalse(self.window.folder_combo.isEnabled())
        credential, client_id, page_instance_id = self._pair_controller(coordinator)
        library = coordinator.library(credential, client_id, page_instance_id)
        manga_id = library["mangas"][0]["id"]
        manga_payload = coordinator.open_manga(
            credential, client_id, manga_id, CompanionActivity.EDIT, page_instance_id
        )["manga"]
        folder_id = manga_payload["folders"][0]["id"]
        folder_payload = coordinator.folder(
            credential,
            client_id,
            folder_id,
            CompanionActivity.EDIT,
            page_instance_id,
        )["folder"]
        image_id = folder_payload["images"][1]["id"]
        coordinator.set_selection(
            credential,
            client_id,
            CompanionActivity.EDIT,
            folder_id,
            image_id,
            True,
            page_instance_id,
        )
        coordinator.set_position(
            credential,
            client_id,
            CompanionActivity.EDIT,
            folder_id,
            image_id,
            page_instance_id,
        )
        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.window.end_companion_mode()
        self.assertEqual(coordinator.status().state, CompanionState.DESKTOP_ACTIVE)
        self.assertEqual(self.window.current_image_index, 1)
        self.assertEqual(self.window.selected_images, {"scan 10.png"})

    def test_companion_read_does_not_move_desktop_editing_resume(self) -> None:
        coordinator, _server = self._replace_window_with_companion()
        manga = self.window.current_manga
        assert manga is not None
        EditingStore(self.root).set_position(
            manga, manga.folders[0].name, manga.folders[0].images[0].name
        )
        with (
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(QMessageBox, "information"),
        ):
            self.window.start_companion_mode()
        credential, client_id, page_instance_id = self._pair_controller(coordinator)
        library = coordinator.library(credential, client_id, page_instance_id)
        manga_id = library["mangas"][0]["id"]
        manga_payload = coordinator.open_manga(
            credential, client_id, manga_id, CompanionActivity.READ, page_instance_id
        )["manga"]
        folder_id = manga_payload["folders"][1]["id"]
        folder_payload = coordinator.folder(
            credential,
            client_id,
            folder_id,
            CompanionActivity.READ,
            page_instance_id,
        )["folder"]
        image_id = folder_payload["images"][0]["id"]
        coordinator.set_position(
            credential,
            client_id,
            CompanionActivity.READ,
            folder_id,
            image_id,
            page_instance_id,
        )
        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.window.end_companion_mode()
        self.assertIsNotNone(self.window.current_manga)
        self.assertIsNotNone(self.window.current_folder)
        assert self.window.current_manga is not None
        assert self.window.current_folder is not None
        self.assertEqual(self.window.current_folder.name, "Arc 2 - Opening")
        self.assertEqual(self.window.current_image_index, 0)
        reading = ReadingStore(self.root).load(self.window.current_manga)
        self.assertEqual(reading.last_folder, "Arc 10 - Finale")


if __name__ == "__main__":
    unittest.main()
