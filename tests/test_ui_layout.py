"""Headless smoke tests for the portrait-first desktop layout."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QByteArray, QEvent, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QCloseEvent, QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from pocket_manga_editor.completion import CompletionBusyError  # noqa: E402
from pocket_manga_editor.exporter import export_selected_pages  # noqa: E402
from pocket_manga_editor.library_lock import LibraryBusyError  # noqa: E402
from pocket_manga_editor.main_window import MainWindow  # noqa: E402


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
