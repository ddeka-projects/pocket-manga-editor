"""PySide6 desktop interface for keyboard-first manga page review."""

from __future__ import annotations

from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from PySide6.QtCore import QByteArray, QSettings, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDesktopServices,
    QImageReader,
    QKeySequence,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .exporter import ExportError, export_selected_pages, output_directory_for
from .models import MangaRef, PageRef, ScanIssue, ScanResult, VolumeRef
from .scanner import ScanError, scan_working_directory
from .storage import SessionStore


class ImageCanvas(QFrame):
    """A fit-to-window image surface with a persistent selection treatment."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("imageCanvas")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setProperty("selected", False)
        self.setMinimumSize(360, 320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._source_pixmap = QPixmap()
        self._page_cache: OrderedDict[str, tuple[QPixmap, str | None]] = OrderedDict()
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_image)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)

        self.image_label = QLabel("Choose a working folder to begin.")
        self.image_label.setObjectName("imageLabel")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setWordWrap(True)
        self.image_label.setMinimumSize(1, 1)
        self.image_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        layout.addWidget(self.image_label)

        self.badge = QLabel("✓  SELECTED", self)
        self.badge.setObjectName("selectedBadge")
        self.badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.badge.adjustSize()
        self.badge.hide()

    def show_page(self, path: Path) -> str | None:
        """Load a page and return an error message if Qt cannot decode it."""

        pixmap, error = self._load_source(path)
        self._source_pixmap = pixmap
        if error is not None:
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText(f"This image could not be displayed.\n\n{path.name}\n{error}")
            return error

        self.image_label.setText("")
        self._render_image()
        return None

    def preload_pages(self, paths: tuple[Path, ...]) -> None:
        """Decode nearby pages while the user is reading the current page."""

        for path in paths:
            self._load_source(path)

    def clear_cache(self) -> None:
        self._page_cache.clear()

    def _load_source(self, path: Path) -> tuple[QPixmap, str | None]:
        key = str(path)
        cached = self._page_cache.pop(key, None)
        if cached is not None:
            self._page_cache[key] = cached
            return cached

        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():
            error = reader.errorString() or "Unknown image decoding error"
            result = (QPixmap(), error)
        else:
            result = (QPixmap.fromImage(image), None)

        self._page_cache[key] = result
        # Current, previous, and next are enough to make ordinary review feel
        # immediate without retaining an entire high-resolution volume.
        while len(self._page_cache) > 3:
            self._page_cache.popitem(last=False)
        return result

    def show_message(self, message: str) -> None:
        self._source_pixmap = QPixmap()
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText(message)
        self.set_selected(False)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.badge.setVisible(selected)
        self._position_badge()
        self.style().unpolish(self)
        self.style().polish(self)
        self.badge.raise_()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt API name
        super().resizeEvent(event)
        self._position_badge()
        if not self._source_pixmap.isNull():
            self._render_timer.start(25)

    def _position_badge(self) -> None:
        self.badge.adjustSize()
        margin = 26
        self.badge.move(max(margin, self.width() - self.badge.width() - margin), margin)

    def _render_image(self) -> None:
        if self._source_pixmap.isNull():
            return

        available = self.image_label.size()
        if available.width() <= 1 or available.height() <= 1:
            return

        device_ratio = self.devicePixelRatioF()
        target = QSize(
            max(1, round(available.width() * device_ratio)),
            max(1, round(available.height() * device_ratio)),
        )
        pixmap = self._source_pixmap.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        pixmap.setDevicePixelRatio(device_ratio)
        self.image_label.setPixmap(pixmap)


class MainWindow(QMainWindow):
    """Main application window and review-session coordinator."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Pocket Manga Editor")
        self.resize(1320, 860)
        self.setMinimumSize(820, 620)

        self.settings = QSettings()
        self.working_directory: Path | None = None
        self.scan_result = ScanResult((), ())
        self.session_store: SessionStore | None = None
        self.current_volume: VolumeRef | None = None
        self.current_index = 0
        self.selected_paths: set[str] = set()
        self._last_output_directory: Path | None = None
        self._session_save_timer = QTimer(self)
        self._session_save_timer.setSingleShot(True)
        self._session_save_timer.timeout.connect(self._save_session)

        self._create_actions()
        self._build_interface()
        self._build_menus()
        self._apply_theme()
        self._restore_window_state()
        self._set_review_enabled(False)
        self.rescan_action.setEnabled(False)
        self.rescan_button.setEnabled(False)
        self.manga_combo.setEnabled(False)
        self.volume_combo.setEnabled(False)

        saved_directory = str(self.settings.value("library/working_directory", ""))
        if saved_directory and Path(saved_directory).is_dir():
            self._set_working_directory(Path(saved_directory))
        else:
            self.canvas.show_message(
                "Choose the folder that contains your manga folders.\n\n"
                "Source images will be reviewed in place and will not be modified."
            )
            QTimer.singleShot(0, self.choose_working_directory)

    def _create_actions(self) -> None:
        self.choose_folder_action = QAction("Choose Working Folder…", self)
        self.choose_folder_action.setShortcut(QKeySequence("Ctrl+O"))
        self.choose_folder_action.triggered.connect(self.choose_working_directory)

        self.rescan_action = QAction("Rescan", self)
        self.rescan_action.setShortcut(QKeySequence("F5"))
        self.rescan_action.triggered.connect(self.rescan)

        self.previous_action = QAction("Previous Page", self)
        self.previous_action.setShortcut(QKeySequence(Qt.Key.Key_Left))
        self.previous_action.triggered.connect(lambda: self.navigate(-1))

        self.next_action = QAction("Next Page", self)
        self.next_action.setShortcut(QKeySequence(Qt.Key.Key_Right))
        self.next_action.triggered.connect(lambda: self.navigate(1))

        self.previous_selected_action = QAction("Previous Selected Page", self)
        self.previous_selected_action.setShortcut(QKeySequence("Ctrl+Left"))
        self.previous_selected_action.triggered.connect(lambda: self.navigate_selected(-1))

        self.next_selected_action = QAction("Next Selected Page", self)
        self.next_selected_action.setShortcut(QKeySequence("Ctrl+Right"))
        self.next_selected_action.triggered.connect(lambda: self.navigate_selected(1))

        self.toggle_action = QAction("Toggle Selection", self)
        self.toggle_action.setShortcut(QKeySequence(Qt.Key.Key_Space))
        self.toggle_action.setAutoRepeat(False)
        self.toggle_action.triggered.connect(self.toggle_current_selection)

        self.select_next_action = QAction("Select and Go to Next Page", self)
        self.select_next_action.setShortcuts(
            [QKeySequence(Qt.Key.Key_Return), QKeySequence(Qt.Key.Key_Enter)]
        )
        self.select_next_action.setAutoRepeat(False)
        self.select_next_action.triggered.connect(self.select_current_and_advance)

        self.first_action = QAction("First Page", self)
        self.first_action.setShortcut(QKeySequence(Qt.Key.Key_Home))
        self.first_action.triggered.connect(lambda: self.go_to_index(0))

        self.last_action = QAction("Last Page", self)
        self.last_action.setShortcut(QKeySequence(Qt.Key.Key_End))
        self.last_action.triggered.connect(self.go_to_last_page)

        self.export_action = QAction("Export Selected Pages…", self)
        self.export_action.setShortcut(QKeySequence("Ctrl+S"))
        self.export_action.setAutoRepeat(False)
        self.export_action.triggered.connect(self.export_selection)

        self.clear_action = QAction("Clear Selections…", self)
        self.clear_action.triggered.connect(self.clear_selections)

        self.help_action = QAction("Keyboard Shortcuts", self)
        self.help_action.setShortcuts([QKeySequence("?"), QKeySequence("F1")])
        self.help_action.setAutoRepeat(False)
        self.help_action.triggered.connect(self.show_keyboard_help)

        self.about_action = QAction("About", self)
        self.about_action.triggered.connect(self.show_about)

        self.quit_action = QAction("Quit", self)
        self.quit_action.setShortcuts(QKeySequence.StandardKey.Quit)
        self.quit_action.triggered.connect(self.close)

        for action in (
            self.choose_folder_action,
            self.rescan_action,
            self.previous_action,
            self.next_action,
            self.previous_selected_action,
            self.next_selected_action,
            self.toggle_action,
            self.select_next_action,
            self.first_action,
            self.last_action,
            self.export_action,
            self.clear_action,
            self.help_action,
        ):
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            self.addAction(action)

    def _build_interface(self) -> None:
        central_widget = QWidget()
        central_widget.setObjectName("centralWidget")
        self.setCentralWidget(central_widget)
        root_layout = QHBoxLayout(central_widget)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(0)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setObjectName("mainSplitter")
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setHandleWidth(7)
        root_layout.addWidget(self.main_splitter)

        self.viewer_panel = QFrame()
        self.viewer_panel.setObjectName("viewerPanel")
        viewer_layout = QVBoxLayout(self.viewer_panel)
        viewer_layout.setContentsMargins(0, 0, 0, 0)
        viewer_layout.setSpacing(0)
        self.canvas = ImageCanvas()
        viewer_layout.addWidget(self.canvas)
        self.main_splitter.addWidget(self.viewer_panel)

        self.sidebar_panel = QFrame()
        self.sidebar_panel.setObjectName("sidebarPanel")
        self.sidebar_panel.setMinimumWidth(350)
        sidebar_panel_layout = QVBoxLayout(self.sidebar_panel)
        sidebar_panel_layout.setContentsMargins(14, 2, 4, 2)
        sidebar_panel_layout.setSpacing(10)

        self.sidebar_scroll = QScrollArea()
        self.sidebar_scroll.setObjectName("sidebarScroll")
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.sidebar_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        sidebar_panel_layout.addWidget(self.sidebar_scroll, 1)

        self.sidebar_content = QWidget()
        self.sidebar_content.setObjectName("sidebarContent")
        self.sidebar_content.setMinimumWidth(320)
        sidebar_layout = QVBoxLayout(self.sidebar_content)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(10)
        self.sidebar_scroll.setWidget(self.sidebar_content)
        self.main_splitter.addWidget(self.sidebar_panel)
        self.main_splitter.setCollapsible(0, False)
        self.main_splitter.setCollapsible(1, False)
        self.main_splitter.setStretchFactor(0, 1)
        self.main_splitter.setStretchFactor(1, 0)

        library_card, library_layout = _sidebar_card("Library")
        self.folder_label = QLabel("No working folder selected")
        self.folder_label.setObjectName("mutedLabel")
        self.folder_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.folder_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        library_layout.addWidget(self.folder_label)

        library_buttons = QHBoxLayout()
        library_buttons.setSpacing(8)
        self.folder_button = QPushButton("Choose Folder")
        self.folder_button.clicked.connect(self.choose_working_directory)
        library_buttons.addWidget(self.folder_button, 1)
        self.rescan_button = QPushButton("Rescan")
        self.rescan_button.clicked.connect(self.rescan)
        library_buttons.addWidget(self.rescan_button)
        library_layout.addLayout(library_buttons)

        self.issues_button = QPushButton("Scan issues")
        self.issues_button.clicked.connect(self.show_scan_issues)
        self.issues_button.hide()
        library_layout.addWidget(self.issues_button)

        manga_label = QLabel("Manga")
        manga_label.setObjectName("fieldLabel")
        library_layout.addWidget(manga_label)
        self.manga_combo = QComboBox()
        self.manga_combo.setMinimumWidth(0)
        self.manga_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.manga_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.manga_combo.setMinimumContentsLength(18)
        self.manga_combo.currentIndexChanged.connect(self._on_manga_changed)
        library_layout.addWidget(self.manga_combo)

        volume_label = QLabel("Volume")
        volume_label.setObjectName("fieldLabel")
        library_layout.addWidget(volume_label)
        self.volume_combo = QComboBox()
        self.volume_combo.setMinimumWidth(0)
        self.volume_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.volume_combo.currentIndexChanged.connect(self._on_volume_changed)
        library_layout.addWidget(self.volume_combo)
        sidebar_layout.addWidget(library_card)

        page_card, page_layout = _sidebar_card("Current page")
        self.progress_label = QLabel("— / —")
        self.progress_label.setObjectName("progressLabel")
        page_layout.addWidget(self.progress_label)
        self.heading_label = QLabel("No volume open")
        self.heading_label.setObjectName("headingLabel")
        self.heading_label.setWordWrap(True)
        self.heading_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        page_layout.addWidget(self.heading_label)

        self.page_label = QLabel("No page selected")
        self.page_label.setObjectName("mutedLabel")
        self.page_label.setWordWrap(True)
        self.page_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.page_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        page_layout.addWidget(self.page_label)

        self.save_status_label = QLabel("Progress autosaves")
        self.save_status_label.setObjectName("saveStatus")
        self.save_status_label.setProperty("error", False)
        page_layout.addWidget(self.save_status_label)
        sidebar_layout.addWidget(page_card)

        review_card, review_layout = _sidebar_card("Review")
        self.selection_label = QLabel("0 selected")
        self.selection_label.setObjectName("selectionPill")
        self.selection_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        review_layout.addWidget(self.selection_label)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(8)
        self.previous_button = QPushButton("←  Previous")
        self.previous_button.clicked.connect(lambda: self.navigate(-1))
        controls_row.addWidget(self.previous_button, 1)
        self.next_button = QPushButton("Next  →")
        self.next_button.clicked.connect(lambda: self.navigate(1))
        controls_row.addWidget(self.next_button, 1)
        review_layout.addLayout(controls_row)

        self.toggle_button = QPushButton("Select Page")
        self.toggle_button.setObjectName("selectButton")
        self.toggle_button.clicked.connect(self.toggle_current_selection)
        review_layout.addWidget(self.toggle_button)

        self.next_selected_button = QPushButton("Next Selected  ⇥")
        self.next_selected_button.clicked.connect(lambda: self.navigate_selected(1))
        review_layout.addWidget(self.next_selected_button)
        sidebar_layout.addWidget(review_card)

        self.export_card, export_layout = _sidebar_card("Export")
        self.export_button = QPushButton("Export Selected")
        self.export_button.setObjectName("primaryButton")
        self.export_button.clicked.connect(self.export_selection)
        export_layout.addWidget(self.export_button)
        self.open_output_button = QPushButton("Open Output")
        self.open_output_button.clicked.connect(self.open_output_directory)
        export_layout.addWidget(self.open_output_button)
        sidebar_layout.addStretch(1)
        sidebar_panel_layout.addWidget(self.export_card)

        self.shortcut_bar = QFrame()
        self.shortcut_bar.setObjectName("shortcutBar")
        shortcut_layout = QGridLayout(self.shortcut_bar)
        shortcut_layout.setContentsMargins(12, 10, 12, 12)
        shortcut_layout.setHorizontalSpacing(8)
        shortcut_layout.setVerticalSpacing(8)
        shortcut_title = QLabel("KEYBOARD")
        shortcut_title.setObjectName("sectionTitle")
        shortcut_layout.addWidget(shortcut_title, 0, 0, 1, 2)
        shortcuts = (
            ("← →", "Navigate"),
            ("Space", "Toggle"),
            ("Enter", "Select + next"),
            ("? / F1", "All shortcuts"),
        )
        for index, (keys, description) in enumerate(shortcuts):
            shortcut_layout.addWidget(
                _shortcut_hint(keys, description), 1 + index // 2, index % 2
            )
        shortcut_layout.setColumnStretch(0, 1)
        shortcut_layout.setColumnStretch(1, 1)
        self.shortcut_bar.setToolTip("Press ? or F1 to show all keyboard controls.")
        sidebar_panel_layout.addWidget(self.shortcut_bar)

        for button in (
            self.folder_button,
            self.rescan_button,
            self.issues_button,
            self.previous_button,
            self.toggle_button,
            self.next_button,
            self.next_selected_button,
            self.export_button,
            self.open_output_button,
        ):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.statusBar().showMessage("Ready")

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(self.choose_folder_action)
        file_menu.addAction(self.rescan_action)
        file_menu.addSeparator()
        file_menu.addAction(self.export_action)
        file_menu.addSeparator()
        file_menu.addAction(self.quit_action)

        review_menu = self.menuBar().addMenu("&Review")
        review_menu.addAction(self.previous_action)
        review_menu.addAction(self.next_action)
        review_menu.addAction(self.previous_selected_action)
        review_menu.addAction(self.next_selected_action)
        review_menu.addAction(self.first_action)
        review_menu.addAction(self.last_action)
        review_menu.addSeparator()
        review_menu.addAction(self.toggle_action)
        review_menu.addAction(self.select_next_action)
        review_menu.addAction(self.clear_action)

        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction(self.help_action)
        help_menu.addAction(self.about_action)

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget#centralWidget {
                background: #17191d;
                color: #eef1f5;
            }
            QFrame#viewerPanel {
                background: #17191d;
                border: none;
            }
            QFrame#sidebarPanel, QScrollArea#sidebarScroll, QWidget#sidebarContent {
                background: #17191d;
                border: none;
            }
            QSplitter#mainSplitter::handle {
                background: #2c3037;
                border-radius: 2px;
                margin: 12px 2px;
            }
            QFrame#sidebarCard, QFrame#shortcutBar {
                background: #202329;
                border: 1px solid #2f343c;
                border-radius: 9px;
            }
            QMenuBar, QMenu {
                background: #202329;
                color: #eef1f5;
            }
            QMenuBar::item:selected, QMenu::item:selected {
                background: #343943;
            }
            QLabel#headingLabel {
                color: #eef1f5;
                font-size: 15px;
                font-weight: 650;
            }
            QLabel#progressLabel {
                color: #ffffff;
                font-size: 27px;
                font-weight: 700;
            }
            QLabel#sectionTitle {
                color: #8f98a5;
                font-size: 11px;
                font-weight: 750;
            }
            QLabel#mutedLabel {
                color: #aeb5c0;
            }
            QLabel#fieldLabel {
                color: #c4cad3;
                font-weight: 600;
            }
            QLabel#selectionPill {
                background: #2a2e35;
                border: 1px solid #414752;
                border-radius: 12px;
                color: #dce1e8;
                font-weight: 650;
                padding: 5px 11px;
            }
            QLabel#saveStatus {
                color: #929aa6;
                padding: 5px 4px;
            }
            QLabel#saveStatus[error="true"] {
                color: #ff7777;
                font-weight: 650;
            }
            QLabel#shortcutKey {
                background: #343943;
                border: 1px solid #59616e;
                border-radius: 4px;
                color: #f5f7fa;
                font-weight: 700;
                padding: 3px 7px;
            }
            QLabel#shortcutDescription {
                color: #aeb5c0;
                font-size: 12px;
            }
            QFrame#imageCanvas {
                background: #0e1013;
                border: 4px solid #2c3037;
                border-radius: 10px;
            }
            QFrame#imageCanvas[selected="true"] {
                border: 4px solid #38c978;
            }
            QLabel#imageLabel {
                border: none;
                color: #aeb5c0;
                font-size: 15px;
            }
            QLabel#selectedBadge {
                background: #159957;
                border: 1px solid #58e19a;
                border-radius: 13px;
                color: white;
                font-weight: 750;
                padding: 6px 11px;
            }
            QPushButton, QComboBox {
                background: #292d34;
                border: 1px solid #454b56;
                border-radius: 6px;
                color: #eef1f5;
                min-height: 30px;
                padding: 2px 11px;
            }
            QPushButton:hover, QComboBox:hover {
                background: #343943;
                border-color: #606875;
            }
            QPushButton:disabled, QComboBox:disabled {
                color: #737984;
                background: #22252a;
                border-color: #30343b;
            }
            QPushButton#selectButton {
                min-width: 125px;
            }
            QPushButton#primaryButton {
                background: #246fe0;
                border-color: #4c8bea;
                font-weight: 650;
            }
            QPushButton#primaryButton:hover {
                background: #327bed;
            }
            QPushButton#primaryButton:disabled {
                background: #273348;
                border-color: #35425a;
            }
            QStatusBar {
                background: #202329;
                color: #aeb5c0;
            }
            QScrollBar:vertical {
                background: #17191d;
                border: none;
                width: 10px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #414752;
                border-radius: 5px;
                min-height: 28px;
            }
            QScrollBar::handle:vertical:hover {
                background: #59616e;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
            """
        )

    def _restore_window_state(self) -> None:
        geometry = self.settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

        splitter_state = self.settings.value("window/main_splitter")
        restored = False
        if isinstance(splitter_state, QByteArray):
            try:
                restored = self.main_splitter.restoreState(splitter_state)
            except (TypeError, ValueError):
                restored = False
        elif isinstance(splitter_state, (bytes, bytearray)):
            try:
                restored = self.main_splitter.restoreState(
                    QByteArray(bytes(splitter_state))
                )
            except (TypeError, ValueError):
                restored = False

        if not restored:
            self._set_default_splitter_sizes()

    def _set_default_splitter_sizes(self) -> None:
        """Give portrait artwork most of the initial window width."""

        total_width = max(self.main_splitter.width(), self.width() - 24)
        sidebar_width = max(350, min(410, round(total_width * 0.3)))
        self.main_splitter.setSizes(
            [max(self.canvas.minimumWidth(), total_width - sidebar_width), sidebar_width]
        )

    def choose_working_directory(self) -> None:
        self._save_session()
        self.settings.sync()
        initial_directory = str(self.working_directory or Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Choose the folder containing your manga folders",
            initial_directory,
        )
        if chosen:
            self._set_working_directory(Path(chosen))

    def _set_working_directory(self, path: Path) -> None:
        # The previous library was saved before this method was called. Clear
        # its in-memory volume before attaching a store rooted in the new
        # library, otherwise the old session could be written into the new one.
        self.current_volume = None
        self.current_index = 0
        self.selected_paths.clear()
        self._last_output_directory = None
        self.scan_result = ScanResult((), ())
        self._clear_review_display("Scanning working folder…")
        self._update_scan_issues()
        self.working_directory = path.resolve()
        self.session_store = SessionStore(self.working_directory)
        self.settings.setValue("library/working_directory", str(self.working_directory))
        self.folder_label.setText(str(self.working_directory))
        self.folder_label.setToolTip(str(self.working_directory))
        self.rescan_action.setEnabled(True)
        self.rescan_button.setEnabled(True)
        self.rescan()

    def rescan(self) -> None:
        if self.working_directory is None:
            return

        self._save_session()
        preferred_manga = self.current_volume.manga_name if self.current_volume else str(
            self.settings.value("library/last_manga", "")
        )
        preferred_volume = (
            self.current_volume.number
            if self.current_volume
            else _setting_decimal(self.settings, "library/last_volume")
        )

        try:
            self.scan_result = scan_working_directory(self.working_directory)
        except ScanError as exc:
            self.current_volume = None
            self.selected_paths.clear()
            self.scan_result = ScanResult((), ())
            self._clear_review_display(str(exc))
            self._update_scan_issues()
            QMessageBox.critical(self, "Could not scan working folder", str(exc))
            self.statusBar().showMessage(str(exc), 8000)
            return

        self._update_scan_issues()
        self._populate_mangas(preferred_manga, preferred_volume)
        manga_count = len(self.scan_result.mangas)
        volume_count = sum(len(manga.volumes) for manga in self.scan_result.mangas)
        self.statusBar().showMessage(
            f"Found {manga_count} manga and {volume_count} volume(s).", 5000
        )

    def _populate_mangas(
        self, preferred_manga: str, preferred_volume: Decimal | None
    ) -> None:
        self.manga_combo.blockSignals(True)
        self.manga_combo.clear()
        for manga in self.scan_result.mangas:
            self.manga_combo.addItem(manga.name, manga)
        self.manga_combo.blockSignals(False)

        if not self.scan_result.mangas:
            self.current_volume = None
            self.selected_paths.clear()
            self.volume_combo.clear()
            self.manga_combo.setEnabled(False)
            self.volume_combo.setEnabled(False)
            self.heading_label.setText("No manga found")
            self.progress_label.setText("— / —")
            self.page_label.setText(
                "Expected folders: Vol. 01, or Vol. 01 Ch. 001 "
                "(chapter name optional)"
            )
            self.canvas.show_message(
                "No matching manga chapters were found in this working folder.\n\n"
                "Use Rescan after adding chapter folders."
            )
            self._set_review_enabled(False)
            return

        preferred_index = 0
        for index, manga in enumerate(self.scan_result.mangas):
            if manga.name == preferred_manga:
                preferred_index = index
                break
        self.manga_combo.blockSignals(True)
        self.manga_combo.setCurrentIndex(preferred_index)
        self.manga_combo.blockSignals(False)
        self.manga_combo.setEnabled(True)
        manga = self.manga_combo.currentData()
        self._populate_volumes(manga, preferred_volume)

    def _on_manga_changed(self, _index: int) -> None:
        manga = self.manga_combo.currentData()
        if not isinstance(manga, MangaRef):
            return
        preferred_volume = (
            _setting_decimal(self.settings, "library/last_volume")
            if manga.name == str(self.settings.value("library/last_manga", ""))
            else None
        )
        self._populate_volumes(manga, preferred_volume)

    def _populate_volumes(
        self, manga: MangaRef, preferred_volume: Decimal | None
    ) -> None:
        self.volume_combo.blockSignals(True)
        self.volume_combo.clear()
        for volume in manga.volumes:
            self.volume_combo.addItem(
                f"{volume.display_name}  ·  {len(volume.pages)} pages",
                volume,
            )

        preferred_index = 0
        for index, volume in enumerate(manga.volumes):
            if preferred_volume is not None and volume.number == preferred_volume:
                preferred_index = index
                break
        self.volume_combo.setCurrentIndex(preferred_index)
        self.volume_combo.blockSignals(False)
        self.volume_combo.setEnabled(bool(manga.volumes))
        self._load_volume(self.volume_combo.currentData())

    def _on_volume_changed(self, _index: int) -> None:
        self._load_volume(self.volume_combo.currentData())

    def _load_volume(self, volume: object) -> None:
        self._save_session()
        if not isinstance(volume, VolumeRef) or self.session_store is None:
            self.current_volume = None
            self._set_review_enabled(False)
            return

        self.current_volume = volume
        self.canvas.clear_cache()
        snapshot = self.session_store.load(volume)
        self.current_index = snapshot.current_index
        self.selected_paths = set(snapshot.selected_paths)
        self._last_output_directory = output_directory_for(volume)
        self._set_save_status(False, "Progress autosaves")
        self.settings.setValue("library/last_manga", volume.manga_name)
        self.settings.setValue("library/last_volume", volume.identity)
        self._set_review_enabled(True)
        self._show_current_page()
        self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)

        if snapshot.warnings:
            self.statusBar().showMessage(" ".join(snapshot.warnings), 10000)

    def _show_current_page(self) -> None:
        volume = self.current_volume
        if volume is None or not volume.pages:
            return

        self.current_index = min(max(self.current_index, 0), len(volume.pages) - 1)
        page = volume.pages[self.current_index]
        display_error = self.canvas.show_page(page.source_path)
        self.canvas.set_selected(page.relative_path in self.selected_paths)
        self.heading_label.setText(f"{volume.manga_name}  ·  {volume.display_name}")
        self.progress_label.setText(f"{self.current_index + 1} / {len(volume.pages)}")
        page_details: list[str] = []
        if page.chapter_number is not None:
            page_details.append(f"Ch. {page.chapter_label}")
            if page.chapter_title:
                page_details.append(page.chapter_title)
        page_details.append(f"Page {page.page_label}")
        page_details.append(page.source_path.name)
        self.page_label.setText("  ·  ".join(page_details))
        self.page_label.setToolTip(str(page.source_path))
        self._refresh_selection_controls()
        QTimer.singleShot(50, self._preload_neighbor_pages)

        if display_error:
            self.statusBar().showMessage(
                f"Could not display {page.relative_path}: {display_error}", 8000
            )

    def _preload_neighbor_pages(self) -> None:
        volume = self.current_volume
        if volume is None:
            return
        neighbor_paths = tuple(
            volume.pages[index].source_path
            for index in (self.current_index + 1, self.current_index - 1)
            if 0 <= index < len(volume.pages)
        )
        self.canvas.preload_pages(neighbor_paths)

    def _refresh_selection_controls(self) -> None:
        volume = self.current_volume
        if volume is None or not volume.pages:
            return

        page = volume.pages[self.current_index]
        is_selected = page.relative_path in self.selected_paths
        selected_count = len(self.selected_paths)
        self.canvas.set_selected(is_selected)
        self.toggle_button.setText("Deselect Page" if is_selected else "Select Page")
        self.selection_label.setText(
            f"✓ {selected_count} selected" if selected_count else "0 selected"
        )
        self.export_button.setText(
            f"Export {selected_count} Selected" if selected_count else "Export Selected"
        )
        self.export_button.setEnabled(selected_count > 0)
        self.export_action.setEnabled(selected_count > 0)
        self.clear_action.setEnabled(selected_count > 0)
        self.previous_selected_action.setEnabled(selected_count > 0)
        self.next_selected_action.setEnabled(selected_count > 0)
        self.next_selected_button.setEnabled(selected_count > 0)

    def navigate(self, offset: int) -> None:
        if self.current_volume is None:
            return
        target = self.current_index + offset
        if target < 0:
            self.statusBar().showMessage("Already at the first page.", 2500)
            return
        if target >= len(self.current_volume.pages):
            self.statusBar().showMessage("Already at the last page.", 2500)
            return
        self.current_index = target
        self._schedule_session_save()
        self._show_current_page()

    def go_to_index(self, index: int) -> None:
        if self.current_volume is None:
            return
        target = min(max(index, 0), len(self.current_volume.pages) - 1)
        if target == self.current_index:
            self.statusBar().showMessage(
                "Already at the first page." if target == 0 else "Already on this page.", 2500
            )
            return
        self.current_index = target
        self._schedule_session_save()
        self._show_current_page()

    def go_to_last_page(self) -> None:
        if self.current_volume is not None:
            self.go_to_index(len(self.current_volume.pages) - 1)

    def navigate_selected(self, direction: int) -> None:
        """Jump among selected pages, wrapping at either end."""

        volume = self.current_volume
        if volume is None or not self.selected_paths:
            return

        selected_indices = [
            index
            for index, page in enumerate(volume.pages)
            if page.relative_path in self.selected_paths
        ]
        if not selected_indices:
            return

        if direction < 0:
            candidates = [index for index in selected_indices if index < self.current_index]
            target = candidates[-1] if candidates else selected_indices[-1]
            wrapped = not candidates
        else:
            candidates = [index for index in selected_indices if index > self.current_index]
            target = candidates[0] if candidates else selected_indices[0]
            wrapped = not candidates

        self.current_index = target
        self._schedule_session_save()
        self._show_current_page()
        if wrapped and len(selected_indices) > 1:
            edge = "last" if direction < 0 else "first"
            self.statusBar().showMessage(f"Wrapped to the {edge} selected page.", 2500)

    def toggle_current_selection(self) -> None:
        volume = self.current_volume
        if volume is None:
            return
        page = volume.pages[self.current_index]
        if page.relative_path in self.selected_paths:
            self.selected_paths.remove(page.relative_path)
            message = f"Deselected {_page_description(page)}."
        else:
            self.selected_paths.add(page.relative_path)
            message = f"Selected {_page_description(page)}."
        self._save_session()
        self._refresh_selection_controls()
        self.statusBar().showMessage(message, 2000)

    def select_current_and_advance(self) -> None:
        volume = self.current_volume
        if volume is None:
            return
        page = volume.pages[self.current_index]
        self.selected_paths.add(page.relative_path)
        if self.current_index < len(volume.pages) - 1:
            self.current_index += 1
            self._save_session()
            self._show_current_page()
        else:
            self._save_session()
            self._refresh_selection_controls()
            self.statusBar().showMessage("Selected the final page of this volume.", 3000)

    def clear_selections(self) -> None:
        if not self.selected_paths or self.current_volume is None:
            return
        answer = QMessageBox.question(
            self,
            "Clear selections",
            f"Clear all {len(self.selected_paths)} selections in "
            f"{self.current_volume.display_name}?\n\n"
            "Previously exported files will not be removed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.selected_paths.clear()
            self._save_session()
            self._refresh_selection_controls()
            self.statusBar().showMessage("Selections cleared.", 3000)

    def export_selection(self) -> None:
        volume = self.current_volume
        if volume is None or not self.selected_paths or self.working_directory is None:
            return

        self._save_session()
        destination = output_directory_for(volume)
        answer = QMessageBox.question(
            self,
            "Export selected pages",
            f"Copy {len(self.selected_paths)} selected page(s) to:\n\n{destination}\n\n"
            "A repeat export removes app-managed pages that are no longer selected. "
            "Unrelated files are preserved.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            result = export_selected_pages(
                self.working_directory, volume, frozenset(self.selected_paths)
            )
        except ExportError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            self.statusBar().showMessage(f"Export failed: {exc}", 10000)
            return

        self._last_output_directory = result.output_directory
        self.open_output_button.setEnabled(True)
        stale_text = (
            f"\nRemoved {result.removed_count} previously exported page(s) "
            "that are no longer selected."
            if result.removed_count
            else ""
        )
        QMessageBox.information(
            self,
            "Export complete",
            f"Exported {result.copied_count} page(s) to:\n\n"
            f"{result.output_directory}{stale_text}",
        )
        self.statusBar().showMessage(
            f"Exported {result.copied_count} selected page(s).", 5000
        )
        self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)

    def open_output_directory(self) -> None:
        if self._last_output_directory is None or not self._last_output_directory.is_dir():
            return
        opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_output_directory)))
        if not opened:
            QMessageBox.warning(
                self,
                "Could not open output folder",
                f"Open this folder manually:\n\n{self._last_output_directory}",
            )

    def _schedule_session_save(self) -> None:
        self._set_save_status(False, "Saving progress…")
        self._session_save_timer.start(300)

    def _save_session(self) -> None:
        self._session_save_timer.stop()
        if self.current_volume is None or self.session_store is None:
            return
        try:
            self.session_store.save(
                self.current_volume, self.current_index, self.selected_paths
            )
        except OSError as exc:
            self._set_save_status(True, "⚠ Progress not saved")
            self.save_status_label.setToolTip(str(exc))
            self.statusBar().showMessage(f"Could not save review progress: {exc}", 10000)
        else:
            self._set_save_status(False, "✓ Progress saved")
            self.save_status_label.setToolTip("")

    def _set_save_status(self, error: bool, text: str) -> None:
        self.save_status_label.setText(text)
        self.save_status_label.setProperty("error", error)
        self.save_status_label.style().unpolish(self.save_status_label)
        self.save_status_label.style().polish(self.save_status_label)

    def _set_review_enabled(self, enabled: bool) -> None:
        for action in (
            self.previous_action,
            self.next_action,
            self.previous_selected_action,
            self.next_selected_action,
            self.toggle_action,
            self.select_next_action,
            self.first_action,
            self.last_action,
        ):
            action.setEnabled(enabled)

        self.previous_button.setEnabled(enabled)
        self.next_button.setEnabled(enabled)
        self.next_selected_button.setEnabled(enabled and bool(self.selected_paths))
        self.toggle_button.setEnabled(enabled)
        has_selection = enabled and bool(self.selected_paths)
        self.export_action.setEnabled(has_selection)
        self.export_button.setEnabled(has_selection)
        self.clear_action.setEnabled(has_selection)
        self.open_output_button.setEnabled(
            enabled
            and self._last_output_directory is not None
            and self._last_output_directory.is_dir()
        )

    def _clear_review_display(self, message: str) -> None:
        self.manga_combo.blockSignals(True)
        self.volume_combo.blockSignals(True)
        self.manga_combo.clear()
        self.volume_combo.clear()
        self.manga_combo.blockSignals(False)
        self.volume_combo.blockSignals(False)
        self.manga_combo.setEnabled(False)
        self.volume_combo.setEnabled(False)
        self.heading_label.setText("No volume open")
        self.progress_label.setText("— / —")
        self.page_label.setText("")
        self.selection_label.setText("0 selected")
        self.canvas.show_message(message)
        self._set_review_enabled(False)

    def _update_scan_issues(self) -> None:
        count = len(self.scan_result.issues)
        self.issues_button.setText(f"Scan issues ({count})")
        self.issues_button.setVisible(count > 0)

    def show_scan_issues(self) -> None:
        if not self.scan_result.issues:
            return
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("Scan issues")
        dialog.setText(
            f"The scan completed with {len(self.scan_result.issues)} non-fatal issue(s)."
        )
        dialog.setInformativeText(
            "Folders or files that did not match the expected structure were skipped."
        )
        dialog.setDetailedText(_format_scan_issues(self.scan_result.issues))
        dialog.exec()

    def show_keyboard_help(self) -> None:
        QMessageBox.information(
            self,
            "Keyboard shortcuts",
            "<h3>Review controls</h3>"
            "<table cellspacing='8'>"
            "<tr><td><b>← / →</b></td><td>Previous or next page</td></tr>"
            "<tr><td><b>Ctrl+← / Ctrl+→</b></td><td>Previous or next selected page</td></tr>"
            "<tr><td><b>Space</b></td><td>Select or deselect the current page</td></tr>"
            "<tr><td><b>Enter</b></td><td>Select the current page and advance</td></tr>"
            "<tr><td><b>Home / End</b></td><td>Go to the first or last page</td></tr>"
            "<tr><td><b>Ctrl+S</b></td><td>Export the current selection</td></tr>"
            "<tr><td><b>F5</b></td><td>Rescan the working folder</td></tr>"
            "<tr><td><b>? / F1</b></td><td>Show this help</td></tr>"
            "</table>"
            "<p>Selections and the current position are saved automatically. "
            "Source JPG and PNG files are copied only when you export.</p>",
        )

    def show_about(self) -> None:
        QMessageBox.about(
            self,
            "About Pocket Manga Editor",
            "<h3>Pocket Manga Editor</h3>"
            "<p>A keyboard-first tool for reviewing manga folders and exporting selected pages.</p>"
            "<p>Development version 0.1.0</p>",
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API name
        self._save_session()
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/main_splitter", self.main_splitter.saveState())
        self.settings.sync()
        event.accept()


def _format_scan_issues(issues: tuple[ScanIssue, ...]) -> str:
    return "\n".join(f"{issue.path}: {issue.message}" for issue in issues)


def _sidebar_card(title: str) -> tuple[QFrame, QVBoxLayout]:
    """Create one visually grouped section in the controls sidebar."""

    card = QFrame()
    card.setObjectName("sidebarCard")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(12, 10, 12, 12)
    layout.setSpacing(7)

    title_label = QLabel(title.upper())
    title_label.setObjectName("sectionTitle")
    layout.addWidget(title_label)
    return card, layout


def _shortcut_hint(keys: str, description: str) -> QWidget:
    """Build one keycap-and-description item for the persistent help bar."""

    item = QWidget()
    layout = QHBoxLayout(item)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(7)

    key_label = QLabel(keys)
    key_label.setObjectName("shortcutKey")
    key_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(key_label)

    description_label = QLabel(description)
    description_label.setObjectName("shortcutDescription")
    layout.addWidget(description_label)
    layout.addStretch(1)
    return item


def _setting_decimal(settings: QSettings, key: str) -> Decimal | None:
    try:
        return Decimal(str(settings.value(key, "")))
    except (InvalidOperation, ValueError):
        return None


def _page_description(page: PageRef) -> str:
    """Return a concise selection message for chapter or direct-volume pages."""

    if page.chapter_number is None:
        return f"page {page.page_label}"
    return f"Ch. {page.chapter_label} page {page.page_label}"
