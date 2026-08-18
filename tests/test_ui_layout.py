"""Headless smoke tests for the portrait-first desktop layout."""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QByteArray, QEvent, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QCloseEvent, QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from pocket_manga_editor.completion import CompletionBusyError  # noqa: E402
from pocket_manga_editor.companion import (  # noqa: E402
    CompanionCoordinator,
    CompanionState,
)
from pocket_manga_editor.exporter import export_selected_pages  # noqa: E402
from pocket_manga_editor.library_lock import LibraryBusyError  # noqa: E402
from pocket_manga_editor.main_window import MainWindow  # noqa: E402


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


class PortraitLayoutTests(unittest.TestCase):
    """Exercise geometry, shortcuts, and saved splitter state without a display."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setOrganizationName("Pocket Manga Editor UI Tests")
        cls.app.setApplicationName("Pocket Manga Editor UI Tests")
        cls.app.setStyle("Fusion")
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.root = root
        QSettings.setPath(
            QSettings.Format.IniFormat,
            QSettings.Scope.UserScope,
            self.temporary.name,
        )

        chapter = root / "Example Manga" / "Vol. 01 Ch. 001 - Opening"
        chapter.mkdir(parents=True)
        image = QImage(600, 1000, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.white)
        self.assertTrue(image.save(str(chapter / "001.png")))
        self.assertTrue(image.save(str(chapter / "002.png")))

        settings = QSettings()
        settings.clear()
        settings.setValue("library/working_directory", str(root))
        settings.sync()

        self.window = MainWindow()

    def tearDown(self) -> None:
        self.window.close()
        self.window.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temporary.cleanup()

    def _show_window(self, width: int = 1280, height: int = 800) -> None:
        self.window.resize(width, height)
        self.window.show()
        QTest.qWait(100)
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

    def test_canvas_dominates_horizontal_split_and_sidebar_scrolls(self) -> None:
        self._show_window()

        self.assertEqual(
            self.window.main_splitter.orientation(), Qt.Orientation.Horizontal
        )
        self.assertIs(self.window.main_splitter.widget(0), self.window.viewer_panel)
        self.assertIs(self.window.main_splitter.widget(1), self.window.sidebar_panel)
        left_width, right_width = self.window.main_splitter.sizes()
        viewer_ratio = left_width / (left_width + right_width)
        self.assertGreaterEqual(viewer_ratio, 0.62)
        self.assertLessEqual(viewer_ratio, 0.75)
        self.assertGreaterEqual(
            self.window.canvas.height(), int(self.window.main_splitter.height() * 0.95)
        )
        self.assertTrue(self.window.sidebar_scroll.widgetResizable())
        self.assertEqual(
            self.window.sidebar_scroll.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        self._assert_pinned_widget_is_visible(self.window.export_card)
        self._assert_pinned_widget_is_visible(self.window.shortcut_bar)

        self.window.resize(820, 620)
        QTest.qWait(50)
        self.assertGreater(self.window.sidebar_scroll.verticalScrollBar().maximum(), 0)
        self._assert_pinned_widget_is_visible(self.window.export_card)
        self._assert_pinned_widget_is_visible(self.window.shortcut_bar)
        self.assertGreater(min(self.window.main_splitter.sizes()), 0)

    def test_review_shortcuts_still_work_after_relocation(self) -> None:
        self._show_window()

        QTest.keyClick(self.window.canvas, Qt.Key.Key_Space)
        self.assertEqual(len(self.window.selected_paths), 1)
        self.assertTrue(self.window.canvas.property("selected"))
        self.assertTrue(self.window.canvas.badge.isVisible())

        QTest.keyClick(self.window.canvas, Qt.Key.Key_Right)
        self.assertEqual(self.window.current_index, 1)
        self.assertEqual(self.window.progress_label.text(), "2 / 2")

    def test_splitter_position_is_restored(self) -> None:
        self._show_window()
        self.window.main_splitter.setSizes([760, 480])
        self.app.processEvents()
        first_sizes = self.window.main_splitter.sizes()
        first_ratio = first_sizes[0] / sum(first_sizes)

        self.window.close()
        self.window.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.window = MainWindow()
        self._show_window()
        restored_sizes = self.window.main_splitter.sizes()
        restored_ratio = restored_sizes[0] / sum(restored_sizes)

        self.assertAlmostEqual(first_ratio, restored_ratio, delta=0.03)

    def test_invalid_splitter_preferences_fall_back_safely(self) -> None:
        for invalid_value in ("not splitter bytes", QByteArray(b"invalid")):
            with self.subTest(value=repr(invalid_value)):
                self.window.close()
                self.window.deleteLater()
                QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()

                settings = QSettings()
                settings.setValue("window/main_splitter", invalid_value)
                settings.sync()
                self.window = MainWindow()
                self._show_window()

                sizes = self.window.main_splitter.sizes()
                self.assertEqual(len(sizes), 2)
                self.assertGreater(min(sizes), 0)
                self.assertGreater(sizes[0], sizes[1])

    def test_busy_save_keeps_outgoing_volume_snapshot_when_switching(self) -> None:
        second_volume = self.root / "Example Manga" / "Vol. 02"
        second_volume.mkdir()
        image = QImage(600, 1000, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.white)
        self.assertTrue(image.save(str(second_volume / "001.png")))
        self.window.rescan()

        manga = self.window.scan_result.mangas[0]
        first, second = manga.volumes
        self.assertEqual(self.window.current_volume, first)
        self.window.current_index = 1
        self.window.selected_paths = {first.pages[1].relative_path}
        store = self.window.session_store
        self.assertIsNotNone(store)
        assert store is not None

        with patch.object(
            store,
            "save",
            side_effect=LibraryBusyError("Another mutation is in progress."),
        ):
            self.window._load_volume(second)

        self.assertEqual(self.window.current_volume, second)
        self.assertTrue(self.window._pending_session_saves)
        self.window._save_session()

        restored = store.load(first)
        self.assertEqual(restored.current_index, 1)
        self.assertEqual(
            restored.selected_paths,
            frozenset({first.pages[1].relative_path}),
        )

    def test_busy_save_defers_close_until_snapshot_is_persisted(self) -> None:
        self._show_window()
        volume = self.window.current_volume
        store = self.window.session_store
        self.assertIsNotNone(volume)
        self.assertIsNotNone(store)
        assert volume is not None
        assert store is not None
        self.window.current_index = 1
        self.window.selected_paths = {volume.pages[1].relative_path}

        event = QCloseEvent()
        with patch.object(
            store,
            "save",
            side_effect=LibraryBusyError("Another mutation is in progress."),
        ):
            self.window.closeEvent(event)

        self.assertFalse(event.isAccepted())
        self.assertTrue(self.window._close_requested)
        self.assertTrue(self.window._pending_session_saves)

        self.window._session_save_timer.stop()
        self.window._flush_pending_session_saves()
        restored = store.load(volume)
        self.assertEqual(restored.current_index, 1)
        self.assertEqual(
            restored.selected_paths,
            frozenset({volume.pages[1].relative_path}),
        )

        self.app.processEvents()
        self.assertFalse(self.window.isVisible())

    def test_companion_handoff_blocks_desktop_and_reloads_mobile_state(self) -> None:
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

        self.assertEqual(
            coordinator.status().state, CompanionState.COMPANION_ACTIVE
        )
        self.assertIs(
            self.window.viewer_stack.currentWidget(),
            self.window.companion_status_panel,
        )
        for control in (
            self.window.folder_button,
            self.window.rescan_button,
            self.window.manga_combo,
            self.window.volume_combo,
            self.window.export_button,
            self.window.complete_button,
        ):
            self.assertFalse(control.isEnabled())

        previous_selection = set(self.window.selected_paths)
        self.window.toggle_current_selection()
        self.assertEqual(self.window.selected_paths, previous_selection)
        with patch(
            "pocket_manga_editor.main_window.scan_working_directory"
        ) as scan:
            self.window.rescan()
        scan.assert_not_called()

        pairing_code = self.window._pairing_code
        self.assertIsNotNone(pairing_code)
        assert pairing_code is not None
        credential = coordinator.pair(pairing_code)
        client_id = "iphone-home-screen"
        coordinator.claim_controller(credential, client_id)
        library = coordinator.library(credential, client_id)
        manga_id = library["mangas"][0]["id"]
        manga = coordinator.manga(credential, client_id, manga_id)["manga"]
        volume_id = manga["volumes"][0]["id"]
        volume = coordinator.volume(credential, client_id, volume_id)["volume"]
        second_page_id = volume["pages"][1]["id"]
        coordinator.set_selection(
            credential,
            client_id,
            volume_id,
            second_page_id,
            True,
        )
        coordinator.set_position(
            credential,
            client_id,
            volume_id,
            second_page_id,
        )

        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.window.end_companion_mode()

        self.assertEqual(coordinator.status().state, CompanionState.DESKTOP_ACTIVE)
        self.assertIs(self.window.viewer_stack.currentWidget(), self.window.canvas)
        self.assertIsNotNone(self.window.current_volume)
        assert self.window.current_volume is not None
        self.assertEqual(self.window.current_index, 1)
        self.assertEqual(
            self.window.selected_paths,
            {self.window.current_volume.pages[1].relative_path},
        )
        self.assertTrue(self.window.folder_button.isEnabled())
        self.assertTrue(self.window.rescan_button.isEnabled())

    def test_close_during_companion_drains_mobile_ownership(self) -> None:
        coordinator, _server = self._replace_window_with_companion()
        self._show_window()
        with (
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(QMessageBox, "information"),
        ):
            self.window.start_companion_mode()
        self.assertEqual(
            coordinator.status().state, CompanionState.COMPANION_ACTIVE
        )

        self.window.close()

        self.assertEqual(coordinator.status().state, CompanionState.DESKTOP_ACTIVE)
        self.assertFalse(self.window.isVisible())

    def test_companion_error_recovery_rescans_before_restoring_desktop(self) -> None:
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

        pairing_code = self.window._pairing_code
        self.assertIsNotNone(pairing_code)
        assert pairing_code is not None
        credential = coordinator.pair(pairing_code)
        client_id = "iphone-recovery"
        coordinator.claim_controller(credential, client_id)
        library = coordinator.library(credential, client_id)
        manga_id = library["mangas"][0]["id"]
        manga = coordinator.manga(credential, client_id, manga_id)["manga"]
        volume_id = manga["volumes"][0]["id"]
        volume = coordinator.volume(credential, client_id, volume_id)["volume"]
        coordinator.set_position(
            credential,
            client_id,
            volume_id,
            volume["pages"][1]["id"],
        )
        coordinator.fail("Simulated Companion failure")

        with patch.object(
            QMessageBox,
            "warning",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.window.end_companion_mode()

        self.assertEqual(coordinator.status().state, CompanionState.DESKTOP_ACTIVE)
        self.assertIs(self.window.viewer_stack.currentWidget(), self.window.canvas)
        self.assertEqual(self.window.current_index, 1)
        self.assertTrue(self.window.folder_button.isEnabled())

    def test_completion_detaches_deleted_manga_before_rescan_and_close(self) -> None:
        volume = self.window.current_volume
        self.assertIsNotNone(volume)
        assert volume is not None
        selected = {volume.pages[0].relative_path}
        export_selected_pages(self.root, volume, selected)
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

        metadata = self.root / ".pocket-manga-editor"
        self.assertFalse((self.root / "Example Manga").exists())
        self.assertTrue(
            (
                metadata
                / "completed"
                / "Example Manga"
                / "Vol.01"
                / "C001_P001.png"
            ).is_file()
        )
        self.assertTrue((metadata / "completed" / "completion-log.json").is_file())
        self.assertFalse((metadata / "selections" / "Example Manga").exists())
        self.assertFalse((metadata / "exports" / "Example Manga").exists())
        self.assertIsNone(self.window.current_volume)

        self.window.close()
        self.app.processEvents()
        self.window._save_session()
        self.assertFalse((metadata / "selections" / "Example Manga").exists())

    def test_busy_completion_keeps_pending_save_retry_active(self) -> None:
        volume = self.window.current_volume
        store = self.window.session_store
        self.assertIsNotNone(volume)
        self.assertIsNotNone(store)
        assert volume is not None
        assert store is not None
        selected = {volume.pages[0].relative_path}
        self.window.selected_paths = selected
        export_selected_pages(self.root, volume, selected)

        with (
            patch.object(
                store,
                "save",
                side_effect=LibraryBusyError("Another mutation is in progress."),
            ),
            patch(
                "pocket_manga_editor.main_window.complete_manga",
                side_effect=CompletionBusyError(
                    "Another library mutation is already in progress."
                ),
            ),
            patch.object(
                QMessageBox,
                "warning",
                return_value=QMessageBox.StandardButton.Yes,
            ),
        ):
            self.window.complete_current_manga()

        self.assertTrue(self.window._pending_session_saves)
        self.assertTrue(self.window._session_save_timer.isActive())

    def test_completion_cancel_at_volume_warning_preserves_everything(self) -> None:
        wrong_output = (
            self.root
            / ".pocket-manga-editor"
            / "output"
            / "Example Manga"
            / "Vol.02"
            / "P001.png"
        )
        wrong_output.parent.mkdir(parents=True)
        wrong_output.write_bytes(b"output")

        with patch.object(
            QMessageBox,
            "warning",
            return_value=QMessageBox.StandardButton.Cancel,
        ) as warning:
            self.window.complete_current_manga()

        self.assertEqual(warning.call_count, 1)
        self.assertTrue((self.root / "Example Manga").is_dir())
        self.assertEqual(wrong_output.read_bytes(), b"output")
        self.assertFalse(
            (
                self.root
                / ".pocket-manga-editor"
                / "completed"
                / "completion-log.json"
            ).exists()
        )

    def test_completion_cancel_at_final_warning_preserves_everything(self) -> None:
        volume = self.window.current_volume
        self.assertIsNotNone(volume)
        assert volume is not None
        output = export_selected_pages(
            self.root, volume, {volume.pages[0].relative_path}
        ).output_directory

        with patch.object(
            QMessageBox,
            "warning",
            return_value=QMessageBox.StandardButton.Cancel,
        ) as warning:
            self.window.complete_current_manga()

        self.assertEqual(warning.call_count, 1)
        self.assertTrue((self.root / "Example Manga").is_dir())
        self.assertTrue(output.is_dir())
        self.assertFalse(
            (
                self.root
                / ".pocket-manga-editor"
                / "completed"
                / "completion-log.json"
            ).exists()
        )

    def _assert_pinned_widget_is_visible(self, widget: object) -> None:
        self.assertTrue(widget.isVisible())
        top_left = widget.mapTo(self.window.sidebar_panel, widget.rect().topLeft())
        bottom_right = widget.mapTo(
            self.window.sidebar_panel, widget.rect().bottomRight()
        )
        self.assertTrue(self.window.sidebar_panel.rect().contains(top_left))
        self.assertTrue(self.window.sidebar_panel.rect().contains(bottom_right))


if __name__ == "__main__":
    unittest.main()
