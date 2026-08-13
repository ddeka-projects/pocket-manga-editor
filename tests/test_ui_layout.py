"""Headless smoke tests for the portrait-first desktop layout."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QByteArray, QEvent, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

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
