"""PySide6 desktop interface for keyboard-first manga image review."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import stat
import time

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
    QApplication,
    QComboBox,
    QDialog,
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
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .companion import (
    CompanionActivity,
    CompanionCoordinator,
    CompanionHTTPService,
    CompanionState,
    MobileContext,
)
from .companion.state import CompanionStateError, DesktopMutationBlocked
from .companion_ui import CompanionConnectionDialog, CompanionStatusPanel
from .completion import (
    CompletionBusyError,
    CompletionError,
    CompletionRecoveryError,
    CompletionRecoveryResult,
    analyze_completion,
    complete_manga,
    recover_interrupted_completions,
)
from .exporter import (
    ExportBusyError,
    ExportError,
    ExportRecoveryError,
    export_manga,
    manga_output_directory,
    recover_interrupted_exports,
)
from .library_lock import LibraryBusyError
from .models import FolderRef, MangaRef, ScanIssue, ScanResult
from .path_safety import is_link_or_reparse
from .scanner import ScanError, scan_working_directory
from .storage import EditingSnapshot, EditingStateError, EditingStore


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
        self._image_cache: OrderedDict[str, tuple[QPixmap, str | None]] = OrderedDict()
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

    def show_image(self, path: Path) -> str | None:
        """Load an image and return an error message if Qt cannot decode it."""

        pixmap, error = self._load_source(path)
        self._source_pixmap = pixmap
        if error is not None:
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText(f"This image could not be displayed.\n\n{path.name}\n{error}")
            return error

        self.image_label.setText("")
        self._render_image()
        return None

    def preload_images(self, paths: tuple[Path, ...]) -> None:
        """Decode nearby images while the user is viewing the current image."""

        for path in paths:
            self._load_source(path)

    def clear_cache(self) -> None:
        self._image_cache.clear()

    def _load_source(self, path: Path) -> tuple[QPixmap, str | None]:
        key = str(path)
        cached = self._image_cache.pop(key, None)
        if cached is not None:
            self._image_cache[key] = cached
            return cached

        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():
            error = reader.errorString() or "Unknown image decoding error"
            result = (QPixmap(), error)
        else:
            result = (QPixmap.fromImage(image), None)

        self._image_cache[key] = result
        # Current, previous, and next are enough to make ordinary review feel
        # immediate without retaining an entire high-resolution folder.
        while len(self._image_cache) > 3:
            self._image_cache.popitem(last=False)
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

    def __init__(
        self,
        *,
        companion_coordinator: CompanionCoordinator | None = None,
        companion_server: CompanionHTTPService | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Pocket Manga Editor")
        self.resize(1320, 860)
        self.setMinimumSize(820, 620)

        self.settings = QSettings()
        self.companion_coordinator = companion_coordinator
        self.companion_server = companion_server
        self._pairing_code: str | None = None
        self._pairing_expires_at: float | None = None
        self._companion_ui_active = False
        self._pending_mobile_context: MobileContext | None = None
        self.working_directory: Path | None = None
        self.scan_result = ScanResult((), ())
        self.editing_store: EditingStore | None = None
        self.current_manga: MangaRef | None = None
        self.current_folder: FolderRef | None = None
        self.current_image_index = 0
        self.selected_images: set[str] = set()
        self.editing_snapshot: EditingSnapshot | None = None
        self._last_output_directory: Path | None = None
        self._pending_session_saves: OrderedDict[
            tuple[str, str, str],
            tuple[EditingStore, MangaRef, FolderRef, str, frozenset[str]],
        ] = OrderedDict()
        self._close_requested = False
        self._session_save_timer = QTimer(self)
        self._session_save_timer.setSingleShot(True)
        self._session_save_timer.timeout.connect(self._save_session)
        self._companion_status_timer = QTimer(self)
        self._companion_status_timer.setInterval(750)
        self._companion_status_timer.timeout.connect(self._refresh_companion_status)
        self._companion_startup_timer = QTimer(self)
        self._companion_startup_timer.setSingleShot(True)
        self._companion_startup_timer.timeout.connect(
            self._start_companion_mode_at_startup
        )

        self._create_actions()
        self._build_interface()
        self._build_menus()
        self._apply_theme()
        self._restore_window_state()
        self._set_review_enabled(False)
        self.rescan_action.setEnabled(False)
        self.rescan_button.setEnabled(False)
        self.manga_combo.setEnabled(False)
        self.folder_combo.setEnabled(False)
        self._refresh_companion_status()
        self._companion_status_timer.start()

        saved_directory = str(self.settings.value("library/working_directory", ""))
        if saved_directory and Path(saved_directory).is_dir():
            self._set_working_directory(Path(saved_directory))
        else:
            self.canvas.show_message(
                "Choose the folder that contains your manga folders.\n\n"
                "Reviewing and exporting do not modify source images. The explicitly "
                "confirmed Complete Manga operation permanently deletes its source folder."
            )
            QTimer.singleShot(0, self.choose_working_directory)
        if (
            self.companion_coordinator is not None
            and self.companion_coordinator.status().paired
        ):
            self._companion_startup_timer.start(0)

    def _create_actions(self) -> None:
        self.choose_folder_action = QAction("Choose Working Folder…", self)
        self.choose_folder_action.setShortcut(QKeySequence("Ctrl+O"))
        self.choose_folder_action.triggered.connect(self.choose_working_directory)

        self.rescan_action = QAction("Rescan", self)
        self.rescan_action.setShortcut(QKeySequence("F5"))
        self.rescan_action.triggered.connect(self.rescan)

        self.companion_action = QAction("Start Companion Mode…", self)
        self.companion_action.triggered.connect(self.start_companion_mode)

        self.previous_action = QAction("Previous Image", self)
        self.previous_action.setShortcut(QKeySequence(Qt.Key.Key_Left))
        self.previous_action.triggered.connect(lambda: self.navigate(-1))

        self.next_action = QAction("Next Image", self)
        self.next_action.setShortcut(QKeySequence(Qt.Key.Key_Right))
        self.next_action.triggered.connect(lambda: self.navigate(1))

        self.previous_selected_action = QAction("Previous Selected Image", self)
        self.previous_selected_action.setShortcut(QKeySequence("Ctrl+Left"))
        self.previous_selected_action.triggered.connect(lambda: self.navigate_selected(-1))

        self.next_selected_action = QAction("Next Selected Image", self)
        self.next_selected_action.setShortcut(QKeySequence("Ctrl+Right"))
        self.next_selected_action.triggered.connect(lambda: self.navigate_selected(1))

        self.toggle_action = QAction("Toggle Selection", self)
        self.toggle_action.setShortcut(QKeySequence(Qt.Key.Key_Space))
        self.toggle_action.setAutoRepeat(False)
        self.toggle_action.triggered.connect(self.toggle_current_selection)

        self.select_next_action = QAction("Select and Go to Next Image", self)
        self.select_next_action.setShortcuts(
            [QKeySequence(Qt.Key.Key_Return), QKeySequence(Qt.Key.Key_Enter)]
        )
        self.select_next_action.setAutoRepeat(False)
        self.select_next_action.triggered.connect(self.select_current_and_advance)

        self.first_action = QAction("First Image", self)
        self.first_action.setShortcut(QKeySequence(Qt.Key.Key_Home))
        self.first_action.triggered.connect(lambda: self.go_to_index(0))

        self.last_action = QAction("Last Image", self)
        self.last_action.setShortcut(QKeySequence(Qt.Key.Key_End))
        self.last_action.triggered.connect(self.go_to_last_image)

        self.export_action = QAction("Export Manga…", self)
        self.export_action.setShortcut(QKeySequence("Ctrl+S"))
        self.export_action.setAutoRepeat(False)
        self.export_action.triggered.connect(self.export_selection)

        self.complete_action = QAction("Complete Manga…", self)
        self.complete_action.setAutoRepeat(False)
        self.complete_action.triggered.connect(self.complete_current_manga)

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
            self.companion_action,
            self.previous_action,
            self.next_action,
            self.previous_selected_action,
            self.next_selected_action,
            self.toggle_action,
            self.select_next_action,
            self.first_action,
            self.last_action,
            self.export_action,
            self.complete_action,
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
        self.viewer_stack = QStackedWidget()
        self.viewer_stack.setObjectName("viewerStack")
        self.canvas = ImageCanvas()
        self.viewer_stack.addWidget(self.canvas)
        self.companion_status_panel = CompanionStatusPanel()
        self.companion_status_panel.pair_requested.connect(self.start_pairing)
        self.companion_status_panel.copy_url_requested.connect(self.copy_companion_url)
        self.companion_status_panel.disconnect_requested.connect(
            self.disconnect_companion_client
        )
        self.companion_status_panel.forget_requested.connect(
            self.forget_companion_device
        )
        self.companion_status_panel.retry_server_requested.connect(
            self.retry_companion_server
        )
        self.companion_status_panel.end_requested.connect(self.end_companion_mode)
        self.viewer_stack.addWidget(self.companion_status_panel)
        viewer_layout.addWidget(self.viewer_stack)
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

        image_folder_label = QLabel("Image folder")
        image_folder_label.setObjectName("fieldLabel")
        library_layout.addWidget(image_folder_label)
        self.folder_combo = QComboBox()
        self.folder_combo.setMinimumWidth(0)
        self.folder_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.folder_combo.currentIndexChanged.connect(self._on_folder_changed)
        library_layout.addWidget(self.folder_combo)
        sidebar_layout.addWidget(library_card)

        companion_card, companion_layout = _sidebar_card("Mobile Companion")
        companion_description = QLabel(
            "Read or edit manga from one paired phone on this local network."
        )
        companion_description.setObjectName("mutedLabel")
        companion_description.setWordWrap(True)
        companion_layout.addWidget(companion_description)
        self.companion_server_label = QLabel("Companion server unavailable")
        self.companion_server_label.setObjectName("saveStatus")
        self.companion_server_label.setWordWrap(True)
        companion_layout.addWidget(self.companion_server_label)
        self.companion_url_label = QLabel("")
        self.companion_url_label.setObjectName("mutedLabel")
        self.companion_url_label.setWordWrap(True)
        self.companion_url_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        companion_layout.addWidget(self.companion_url_label)
        self.companion_button = QPushButton("Start Companion Mode…")
        self.companion_button.setObjectName("primaryButton")
        self.companion_button.clicked.connect(self.start_companion_mode)
        companion_layout.addWidget(self.companion_button)
        companion_buttons = QHBoxLayout()
        companion_buttons.setSpacing(8)
        self.companion_pair_button = QPushButton("Pair Phone")
        self.companion_pair_button.clicked.connect(self.manage_companion_device)
        companion_buttons.addWidget(self.companion_pair_button, 1)
        self.companion_settings_button = QPushButton("Connection…")
        self.companion_settings_button.clicked.connect(
            self.configure_companion_connection
        )
        companion_buttons.addWidget(self.companion_settings_button, 1)
        companion_layout.addLayout(companion_buttons)
        sidebar_layout.addWidget(companion_card)

        image_card, image_layout = _sidebar_card("Current image")
        self.progress_label = QLabel("— / —")
        self.progress_label.setObjectName("progressLabel")
        image_layout.addWidget(self.progress_label)
        self.heading_label = QLabel("No image folder open")
        self.heading_label.setObjectName("headingLabel")
        self.heading_label.setWordWrap(True)
        self.heading_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        image_layout.addWidget(self.heading_label)

        self.image_name_label = QLabel("No image selected")
        self.image_name_label.setObjectName("mutedLabel")
        self.image_name_label.setWordWrap(True)
        self.image_name_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.image_name_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        image_layout.addWidget(self.image_name_label)

        self.save_status_label = QLabel("Progress autosaves")
        self.save_status_label.setObjectName("saveStatus")
        self.save_status_label.setProperty("error", False)
        image_layout.addWidget(self.save_status_label)
        sidebar_layout.addWidget(image_card)

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

        self.toggle_button = QPushButton("Select Image")
        self.toggle_button.setObjectName("selectButton")
        self.toggle_button.clicked.connect(self.toggle_current_selection)
        review_layout.addWidget(self.toggle_button)

        self.next_selected_button = QPushButton("Next Selected  ⇥")
        self.next_selected_button.clicked.connect(lambda: self.navigate_selected(1))
        review_layout.addWidget(self.next_selected_button)
        sidebar_layout.addWidget(review_card)

        self.export_card, export_layout = _sidebar_card("Export")
        self.export_button = QPushButton("Export Manga")
        self.export_button.setObjectName("primaryButton")
        self.export_button.clicked.connect(self.export_selection)
        export_layout.addWidget(self.export_button)
        self.open_output_button = QPushButton("Open Output")
        self.open_output_button.clicked.connect(self.open_output_directory)
        export_layout.addWidget(self.open_output_button)
        self.complete_button = QPushButton("Complete Manga…")
        self.complete_button.setObjectName("dangerButton")
        self.complete_button.setToolTip(
            "Move this manga's current output into a completed batch, then permanently "
            "delete its source folder and saved review data."
        )
        self.complete_button.clicked.connect(self.complete_current_manga)
        export_layout.addWidget(self.complete_button)
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
            ("Enter", "Select + next image"),
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
            self.companion_button,
            self.companion_pair_button,
            self.companion_settings_button,
            self.previous_button,
            self.toggle_button,
            self.next_button,
            self.next_selected_button,
            self.export_button,
            self.open_output_button,
            self.complete_button,
        ):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.statusBar().showMessage("Ready")

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(self.choose_folder_action)
        file_menu.addAction(self.rescan_action)
        file_menu.addAction(self.companion_action)
        file_menu.addSeparator()
        file_menu.addAction(self.export_action)
        file_menu.addAction(self.complete_action)
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
            QFrame#companionStatusPanel {
                background: #0e1013;
                border: 1px solid #2c3037;
                border-radius: 10px;
            }
            QFrame#companionStatusCard {
                background: #202329;
                border: 1px solid #3c444e;
                border-radius: 12px;
            }
            QLabel#companionEyebrow {
                color: #52da91;
                font-size: 11px;
                font-weight: 800;
            }
            QLabel#companionTitle {
                color: #ffffff;
                font-size: 25px;
                font-weight: 750;
            }
            QLabel#companionDetail {
                color: #f0f3f6;
                font-weight: 650;
            }
            QLabel#companionError {
                background: #4a2025;
                border: 1px solid #8a3c45;
                border-radius: 6px;
                color: #ffb6bd;
                padding: 9px;
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
            QPushButton#dangerButton {
                background: #3a2428;
                border-color: #704047;
                color: #ffdce1;
                font-weight: 650;
            }
            QPushButton#dangerButton:hover {
                background: #522d34;
                border-color: #9a535e;
            }
            QPushButton#dangerButton:disabled {
                background: #282428;
                border-color: #3b3439;
                color: #7f7479;
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

    def _desktop_mutation_allowed(self, *, notify: bool = True) -> bool:
        """Enforce ownership independently of whether a widget is disabled."""

        coordinator = self.companion_coordinator
        if coordinator is None:
            return True
        try:
            coordinator.require_desktop_mutation()
        except DesktopMutationBlocked as exc:
            if notify:
                self.statusBar().showMessage(str(exc), 5000)
            return False
        return True

    def start_companion_mode(self) -> None:
        """Flush desktop state and transfer review ownership to the HTTP service."""

        self._enter_companion_mode(automatic=False)

    def _start_companion_mode_at_startup(self) -> None:
        """Enter Companion ownership when a remembered phone is available."""

        coordinator = self.companion_coordinator
        server = self.companion_server
        if coordinator is None or server is None:
            return
        status = coordinator.status()
        if (
            not status.paired
            or status.last_error is not None
            or status.state is not CompanionState.DESKTOP_ACTIVE
            or self.working_directory is None
            or not self.scan_result.mangas
            or not server.running
        ):
            return
        self._enter_companion_mode(automatic=True)

    def _enter_companion_mode(self, *, automatic: bool) -> bool:
        """Run the shared safe handoff for manual and automatic entry."""

        coordinator = self.companion_coordinator
        server = self.companion_server
        if coordinator is None or server is None:
            if not automatic:
                QMessageBox.warning(
                    self,
                    "Companion unavailable",
                    "The Companion service was not initialized for this application run.",
                )
            return False
        state = coordinator.status().state
        if state is CompanionState.COMPANION_ACTIVE:
            self.viewer_stack.setCurrentWidget(self.companion_status_panel)
            return True
        if state is not CompanionState.DESKTOP_ACTIVE:
            if not automatic:
                QMessageBox.warning(
                    self,
                    "Companion needs attention",
                    "Companion Mode is in a transition or error state. Use the status "
                    "panel to finish or recover the handoff before trying again.",
                )
            self._set_companion_ui(True)
            return False
        if (
            self.working_directory is None
            or not self.scan_result.mangas
            or not server.running
        ):
            detail = server.error or (
                "Choose a working folder containing at least one readable manga."
                if not self.scan_result.mangas
                else "Start or retry the Companion server first."
            )
            if automatic:
                self.statusBar().showMessage(
                    f"Automatic Companion Mode was not started: {detail}", 10000
                )
            else:
                QMessageBox.warning(self, "Cannot start Companion Mode", detail)
            return False

        if not automatic:
            answer = QMessageBox.question(
                self,
                "Start Companion Mode",
                "Transfer review control to one paired phone?\n\n"
                "Desktop navigation, selections, export, completion, rescan, and "
                "working-folder changes will be locked until Companion Mode ends.\n\n"
                f"Mobile address:\n{server.url}"
                + (
                    "\n\nOnly a PC-local address was detected. Configure this PC's "
                    "reserved LAN address under Connection… before opening it on the phone."
                    if not server.status().lan_address_available
                    else ""
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False

        if not self._save_session() or self._pending_session_saves:
            detail = (
                "Companion Mode was not started because desktop progress could not "
                "be saved safely. Wait for the current library operation and try again."
            )
            if automatic:
                self.statusBar().showMessage(detail, 10000)
            else:
                QMessageBox.warning(
                    self,
                    "Progress has not finished saving",
                    detail,
                )
            return False
        rescanned, _recovery = self._rescan(require_save_success=True)
        if not rescanned or not self.scan_result.mangas:
            detail = "The library could not be refreshed into a non-empty snapshot."
            if automatic:
                self.statusBar().showMessage(
                    f"Automatic Companion Mode was not started: {detail}", 10000
                )
            else:
                QMessageBox.warning(self, "Cannot start Companion Mode", detail)
            return False

        try:
            coordinator.enter_companion(self.working_directory, self.scan_result)
        except Exception as exc:  # The coordinator has already failed closed.
            self._set_companion_ui(True)
            self._refresh_companion_status()
            QMessageBox.critical(
                self,
                "Companion handoff failed",
                f"Desktop and mobile writes remain blocked until recovery.\n\n{exc}",
            )
            return False

        self._session_save_timer.stop()
        self._set_companion_ui(True)
        self._refresh_companion_status()
        if not coordinator.status().paired:
            self.start_pairing()
        self.statusBar().showMessage(
            (
                "Companion Mode started automatically; review ownership is on the phone."
                if automatic
                else "Companion Mode active; review ownership is on the phone."
            ),
            8000,
        )
        return True

    def end_companion_mode(self) -> None:
        """Drain mobile writes, reload editing state, then restore desktop ownership."""

        coordinator = self.companion_coordinator
        if coordinator is None:
            return
        state = coordinator.status().state
        if state is CompanionState.COMPANION_ERROR:
            answer = QMessageBox.warning(
                self,
                "Recover desktop ownership",
                "Companion Mode reported an error. Mobile writes are blocked. "
                "Recover desktop mode and rescan the library now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            try:
                self._pending_mobile_context = coordinator.begin_recovery()
            except CompanionStateError as exc:
                QMessageBox.critical(self, "Recovery failed", str(exc))
                return
            self._remember_mobile_context(self._pending_mobile_context)
            self.current_manga = None
            self.current_folder = None
            self.current_image_index = 0
            self.selected_images.clear()
            self.editing_snapshot = None
            rescanned, _recovery = self._rescan()
            if rescanned:
                try:
                    coordinator.finish_recovery()
                except CompanionStateError as exc:
                    QMessageBox.critical(self, "Recovery failed", str(exc))
                    self._set_companion_ui(True)
                    self._refresh_companion_status()
                    return
                self._pending_mobile_context = None
                self._set_companion_ui(False)
                self._set_review_enabled(self.current_folder is not None)
            else:
                self._set_companion_ui(True)
            self._refresh_companion_status()
            return
        if state is CompanionState.EXITING_COMPANION:
            self._reload_desktop_after_companion()
            return
        if state is not CompanionState.COMPANION_ACTIVE:
            return

        answer = QMessageBox.question(
            self,
            "End Companion Mode",
            "Return review control to this desktop?\n\nThe final confirmed phone "
            "position and selections will be reloaded from saved progress.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._pending_mobile_context = coordinator.begin_exit()
        except Exception as exc:
            self._set_companion_ui(True)
            self._refresh_companion_status()
            QMessageBox.critical(
                self,
                "Could not end Companion Mode",
                f"Mobile and desktop writes remain blocked.\n\n{exc}",
            )
            return
        self._reload_desktop_after_companion()

    def _reload_desktop_after_companion(self) -> None:
        coordinator = self.companion_coordinator
        if coordinator is None:
            return
        self._remember_mobile_context(self._pending_mobile_context)

        self._session_save_timer.stop()
        self.current_manga = None
        self.current_folder = None
        self.current_image_index = 0
        self.selected_images.clear()
        self.editing_snapshot = None
        self._last_output_directory = None
        self.canvas.clear_cache()
        rescanned, _recovery = self._rescan()
        if not rescanned:
            QMessageBox.critical(
                self,
                "Desktop reload failed",
                "Mobile writes are stopped, but desktop controls remain locked because "
                "the library could not be reloaded. Correct the reported problem and "
                "choose End Companion Mode again.",
            )
            self._set_companion_ui(True)
            self._refresh_companion_status()
            return
        try:
            coordinator.finish_exit()
        except CompanionStateError as exc:
            coordinator.fail(str(exc))
            QMessageBox.critical(self, "Companion exit failed", str(exc))
            self._set_companion_ui(True)
            self._refresh_companion_status()
            return
        self._pending_mobile_context = None
        self._set_companion_ui(False)
        self._set_review_enabled(self.current_folder is not None)
        self._refresh_companion_status()
        self.statusBar().showMessage(
            "Companion Mode ended; desktop progress was reloaded.", 6000
        )

    def _remember_mobile_context(self, context: MobileContext | None) -> None:
        """Resume desktop editing only from a confirmed phone Edit context."""

        if context is not None and context.activity is CompanionActivity.EDIT:
            self.settings.setValue("library/last_manga", context.manga_name)

    def start_pairing(self) -> None:
        coordinator = self.companion_coordinator
        server = self.companion_server
        if coordinator is None or server is None or not server.running:
            QMessageBox.warning(
                self, "Pairing unavailable", "The Companion server is not running."
            )
            return
        try:
            offer = coordinator.start_pairing()
        except Exception as exc:
            QMessageBox.critical(self, "Could not open pairing", str(exc))
            return
        self._pairing_code = offer.code
        self._pairing_expires_at = offer.expires_at
        self._refresh_companion_status()
        QMessageBox.information(
            self,
            "Pair your phone",
            f"On the same trusted network, open:\n\n{server.url}\n\n"
            f"Enter this one-time code:  {offer.code}\n\n"
            "The code expires in five minutes. The phone will be remembered until "
            "you choose Forget Paired Device."
            + (
                "\n\nThe displayed address is PC-only. Configure this PC's reserved "
                "LAN address under Connection… before opening it on the phone."
                if not server.status().lan_address_available
                else ""
            ),
        )

    def manage_companion_device(self) -> None:
        coordinator = self.companion_coordinator
        if coordinator is not None and coordinator.status().paired:
            self.forget_companion_device()
        else:
            self.start_pairing()

    def copy_companion_url(self) -> None:
        server = self.companion_server
        if server is None:
            return
        QApplication.clipboard().setText(server.url)
        self.statusBar().showMessage("Companion URL copied.", 2500)

    def disconnect_companion_client(self) -> None:
        coordinator = self.companion_coordinator
        if coordinator is None:
            return
        coordinator.disconnect_client()
        self._refresh_companion_status()
        self.statusBar().showMessage(
            "Mobile controller disconnected; Companion Mode remains active.", 5000
        )

    def forget_companion_device(self) -> None:
        coordinator = self.companion_coordinator
        if coordinator is None or not coordinator.status().paired:
            return
        answer = QMessageBox.question(
            self,
            "Forget paired device",
            "Forget the paired phone and disconnect its current controller lease?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            coordinator.forget_device()
        except OSError as exc:
            QMessageBox.critical(self, "Could not forget device", str(exc))
            return
        self._pairing_code = None
        self._pairing_expires_at = None
        self._refresh_companion_status()

    def configure_companion_connection(self) -> None:
        server = self.companion_server
        if server is None or not self._desktop_mutation_allowed():
            return
        configured_host = str(self.settings.value("companion/public_host", ""))
        dialog = CompanionConnectionDialog(
            public_host=configured_host,
            port=server.status().port,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        public_host, port = dialog.values()
        try:
            status = server.restart(port=port, public_host=public_host or "")
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Companion address", str(exc))
            return
        self.settings.setValue("companion/public_host", public_host or "")
        self.settings.setValue("companion/port", port)
        self.settings.sync()
        self._refresh_companion_status()
        if not status.running:
            QMessageBox.warning(
                self,
                "Companion server unavailable",
                status.error or "The Companion server could not be started.",
            )

    def retry_companion_server(self) -> None:
        server = self.companion_server
        if server is None:
            return
        status = server.start()
        self._refresh_companion_status()
        if not status.running:
            QMessageBox.warning(
                self,
                "Companion server unavailable",
                status.error or "The Companion server could not be started.",
            )

    def _set_companion_ui(self, active: bool) -> None:
        self._companion_ui_active = active
        self.viewer_stack.setCurrentWidget(
            self.companion_status_panel if active else self.canvas
        )
        self.choose_folder_action.setEnabled(not active)
        self.folder_button.setEnabled(not active)
        self.rescan_action.setEnabled(not active and self.working_directory is not None)
        self.rescan_button.setEnabled(not active and self.working_directory is not None)
        self.manga_combo.setEnabled(not active and bool(self.scan_result.mangas))
        self.folder_combo.setEnabled(not active and self.current_folder is not None)
        self.issues_button.setEnabled(not active)
        self._set_review_enabled(not active and self.current_folder is not None)

    def _refresh_companion_status(self) -> None:
        coordinator = self.companion_coordinator
        server = self.companion_server
        if coordinator is None or server is None:
            self.companion_server_label.setText("Companion server unavailable")
            self.companion_url_label.setText("")
            self.companion_button.setEnabled(False)
            self.companion_pair_button.setEnabled(False)
            self.companion_settings_button.setEnabled(False)
            self.companion_action.setEnabled(False)
            self.companion_status_panel.update_status(
                url=None,
                paired=False,
                pairing_code=None,
                pairing_expires_text=None,
                client_name=None,
                context=None,
                selected_count=None,
                server_error="The Companion service is unavailable.",
                server_retry_available=False,
            )
            return

        service_status = server.status()
        status = coordinator.status()
        if self._pairing_expires_at is not None and time.time() >= self._pairing_expires_at:
            self._pairing_code = None
            self._pairing_expires_at = None
        expiry_text = (
            time.strftime("%H:%M", time.localtime(self._pairing_expires_at))
            if self._pairing_expires_at is not None
            else None
        )
        context_text = None
        if status.mobile_context is not None:
            context_text = (
                f"{status.mobile_context.activity.value.title()} · "
                f"{status.mobile_context.manga_name} · "
                f"{status.mobile_context.folder_name} · "
                f"{status.mobile_context.image_name}"
            )
        client_text = (
            "Connected phone"
            if status.active_client
            else ("Phone reconnecting" if status.active_client_id else None)
        )
        displayed_url = service_status.url if service_status.running else None
        if displayed_url and not service_status.lan_address_available:
            displayed_url += "\nPC only · configure a reserved LAN address"
        self.companion_status_panel.update_status(
            url=displayed_url,
            paired=status.paired,
            pairing_code=self._pairing_code if status.pairing_open else None,
            pairing_expires_text=expiry_text,
            client_name=client_text,
            context=context_text,
            selected_count=(
                status.mobile_context.selected_count
                if status.mobile_context is not None
                else None
            ),
            server_error=service_status.error or status.last_error,
            server_retry_available=bool(
                service_status.error or not service_status.running
            ),
        )
        self.companion_server_label.setText(
            (
                f"● Listening on port {service_status.port}"
                if service_status.lan_address_available
                else f"● Listening on port {service_status.port} · LAN address needed"
            )
            if service_status.running
            else "⚠ Companion server unavailable"
        )
        self.companion_url_label.setText(
            displayed_url if service_status.running else (service_status.error or "")
        )
        desktop_active = status.state is CompanionState.DESKTOP_ACTIVE
        if status.state is CompanionState.COMPANION_ACTIVE:
            self.companion_status_panel.update_mode(
                "Review ownership is on your phone",
                "Desktop review, export, completion, rescan, and folder changes "
                "stay locked until Companion Mode ends.",
            )
        elif status.state is CompanionState.EXITING_COMPANION:
            self.companion_status_panel.update_mode(
                "Returning ownership to the desktop",
                "Phone writes are stopped while saved progress is rescanned and "
                "reloaded. Desktop controls remain locked until that succeeds.",
            )
        elif status.state is CompanionState.COMPANION_ERROR:
            self.companion_status_panel.update_mode(
                "Companion Mode needs recovery",
                "Both phone and desktop writes are blocked. Resolve the reported "
                "problem, then recover desktop mode safely.",
            )
        has_library = self.working_directory is not None and bool(self.scan_result.mangas)
        can_start = desktop_active and service_status.running and has_library
        self.companion_action.setEnabled(can_start)
        self.companion_button.setEnabled(can_start)
        self.companion_button.setText(
            "Start Companion Mode…"
            if desktop_active
            else (
                "Companion Mode Active"
                if status.state is CompanionState.COMPANION_ACTIVE
                else "Companion Needs Attention"
            )
        )
        self.companion_pair_button.setEnabled(service_status.running)
        self.companion_pair_button.setText(
            "Forget Phone" if status.paired else "Pair Phone"
        )
        self.companion_settings_button.setEnabled(desktop_active)
        if status.state is not CompanionState.DESKTOP_ACTIVE:
            self._set_companion_ui(True)
        self.companion_status_panel.end_button.setText(
            "End Companion Mode"
            if status.state is CompanionState.COMPANION_ACTIVE
            else (
                "Retry Desktop Reload"
                if status.state is CompanionState.EXITING_COMPANION
                else "Recover Desktop Mode"
            )
        )

    def choose_working_directory(self) -> None:
        if not self._desktop_mutation_allowed():
            return
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
        if not self._desktop_mutation_allowed():
            return
        raw_path = Path(path).expanduser()
        try:
            if is_link_or_reparse(raw_path):
                raise OSError(
                    "The working directory cannot be a symbolic link or junction."
                )
            information = raw_path.stat(follow_symlinks=False)
            resolved_path = raw_path.resolve(strict=True)
            if not stat.S_ISDIR(information.st_mode):
                raise OSError("The working directory is not a folder.")
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Could not use working folder",
                f"{raw_path}\n\n{exc}",
            )
            self.statusBar().showMessage(f"Could not use working folder: {exc}", 10000)
            return

        # Detach the previous library before attaching a store rooted in the
        # new one, otherwise an old immutable autosave could target this root.
        self.current_manga = None
        self.current_folder = None
        self.current_image_index = 0
        self.selected_images.clear()
        self.editing_snapshot = None
        self._last_output_directory = None
        self.scan_result = ScanResult((), ())
        self._clear_review_display("Scanning working folder…")
        self._update_scan_issues()
        self.working_directory = resolved_path
        self.editing_store = EditingStore(self.working_directory)
        self.settings.setValue("library/working_directory", str(self.working_directory))
        self.folder_label.setText(str(self.working_directory))
        self.folder_label.setToolTip(str(self.working_directory))
        self.rescan_action.setEnabled(True)
        self.rescan_button.setEnabled(True)
        self.rescan()

    def rescan(self) -> None:
        if not self._desktop_mutation_allowed():
            return
        self._rescan()

    def _rescan(
        self,
        *,
        show_recovery_dialog: bool = True,
        require_save_success: bool = False,
    ) -> tuple[bool, CompletionRecoveryResult | None]:
        if self.working_directory is None:
            return False, None

        # Capture the preferred location before recovery.  An interrupted
        # transaction may make the in-memory editing snapshot stale, so no
        # autosave may run until both recovery passes have inspected their
        # journals under the library lock.
        preferred_manga = self.current_manga.name if self.current_manga else str(
            self.settings.value("library/last_manga", "")
        )
        export_recovery = None
        try:
            # Output and editing.json must be reconciled before completion
            # recovery or the scanner observes either managed tree.
            export_recovery = recover_interrupted_exports(self.working_directory)
            recovery = recover_interrupted_completions(self.working_directory)
        except (ExportBusyError, CompletionBusyError) as exc:
            self._session_save_timer.stop()
            self._pending_session_saves.clear()
            self.current_manga = None
            self.current_folder = None
            self.current_image_index = 0
            self.selected_images.clear()
            self.editing_snapshot = None
            self._last_output_directory = None
            self.canvas.clear_cache()
            self.scan_result = ScanResult((), ())
            self._clear_review_display(
                "The library is busy in another Pocket Manga Editor window."
            )
            self._update_scan_issues()
            QMessageBox.warning(
                self,
                "Library busy",
                f"{exc}\n\nNo scan was started while another window was changing "
                "the library. Try Rescan again shortly.",
            )
            self.statusBar().showMessage("Library busy; try Rescan again shortly.", 8000)
            return False, None
        except (ExportRecoveryError, CompletionError) as exc:
            # A recovery failure means source/output paths may be between their
            # active and staged locations. Never scan or autosave against that
            # uncertain filesystem state.
            self._session_save_timer.stop()
            self._pending_session_saves.clear()
            self.current_manga = None
            self.current_folder = None
            self.current_image_index = 0
            self.selected_images.clear()
            self.editing_snapshot = None
            self._last_output_directory = None
            self.canvas.clear_cache()
            self.scan_result = ScanResult((), ())
            operation = (
                "export" if isinstance(exc, ExportRecoveryError) else "completion"
            )
            self._clear_review_display(
                f"An interrupted manga {operation} could not be recovered safely."
            )
            self._update_scan_issues()
            QMessageBox.critical(
                self,
                f"{operation.title()} recovery failed",
                f"{exc}\n\nThe library was not scanned because files may be in "
                "transition. Close other Pocket Manga Editor windows, then use "
                "Rescan. If the problem remains, inspect the transaction folder "
                "reported by the error.",
            )
            self.statusBar().showMessage(
                f"{operation.title()} recovery failed: {exc}", 15000
            )
            return False, None

        recovered_state = bool(
            (export_recovery is not None and export_recovery.recovered_count)
            or recovery.recovered_count
        )
        if recovered_state:
            # The recovered files, not a snapshot captured before recovery,
            # are authoritative.  Detach and discard every queued immutable
            # save before loading them again below.
            self._session_save_timer.stop()
            self._pending_session_saves.clear()
            self.current_manga = None
            self.current_folder = None
            self.current_image_index = 0
            self.selected_images.clear()
            self.editing_snapshot = None
            self._last_output_directory = None
            self.canvas.clear_cache()
        else:
            save_succeeded = self._save_session()
            if require_save_success and not save_succeeded:
                self.statusBar().showMessage(
                    "Library scan stopped because review progress was not saved.", 10000
                )
                return False, None

        try:
            self.scan_result = scan_working_directory(self.working_directory)
        except ScanError as exc:
            self.current_manga = None
            self.current_folder = None
            self.selected_images.clear()
            self.editing_snapshot = None
            self.scan_result = ScanResult((), ())
            self._clear_review_display(str(exc))
            self._update_scan_issues()
            QMessageBox.critical(self, "Could not scan working folder", str(exc))
            self.statusBar().showMessage(str(exc), 8000)
            return False, recovery

        self._update_scan_issues()
        self._populate_mangas(preferred_manga)
        manga_count = len(self.scan_result.mangas)
        folder_count = sum(len(manga.folders) for manga in self.scan_result.mangas)
        self.statusBar().showMessage(
            f"Found {manga_count} manga and {folder_count} image folder(s).", 5000
        )
        export_recovered = bool(
            export_recovery is not None and export_recovery.recovered_count
        )
        if show_recovery_dialog and (
            export_recovered
            or recovery.recovered_count
            or recovery.warnings
        ):
            recovery_lines: list[str] = []
            if export_recovery is not None:
                if export_recovery.committed_count:
                    recovery_lines.append(
                        f"Finished {export_recovery.committed_count} interrupted export(s)."
                    )
                if export_recovery.rolled_back_count:
                    recovery_lines.append(
                        f"Restored {export_recovery.rolled_back_count} interrupted export(s)."
                    )
                if export_recovery.discarded_count:
                    recovery_lines.append(
                        f"Removed {export_recovery.discarded_count} empty export journal(s)."
                    )
            if recovery.rolled_back_count:
                recovery_lines.append(
                    f"Restored {recovery.rolled_back_count} interrupted completion(s)."
                )
            if recovery.cleaned_count:
                recovery_lines.append(
                    f"Finished cleanup for {recovery.cleaned_count} committed "
                    "completion(s)."
                )
            recovery_lines.extend(recovery.warnings)
            QMessageBox.warning(
                self,
                "Interrupted operation recovered",
                "\n".join(recovery_lines)
                + "\n\nReview the restored library before continuing.",
            )
        self._refresh_companion_status()
        return True, recovery

    def _populate_mangas(self, preferred_manga: str) -> None:
        self.manga_combo.blockSignals(True)
        self.manga_combo.clear()
        for manga in self.scan_result.mangas:
            self.manga_combo.addItem(manga.name, manga)
        self.manga_combo.blockSignals(False)

        if not self.scan_result.mangas:
            self.current_manga = None
            self.current_folder = None
            self.selected_images.clear()
            self.editing_snapshot = None
            self.folder_combo.clear()
            self.manga_combo.setEnabled(False)
            self.folder_combo.setEnabled(False)
            self.heading_label.setText("No manga found")
            self.progress_label.setText("— / —")
            self.image_name_label.setText(
                "Each manga needs a direct folder containing JPG or PNG images."
            )
            self.canvas.show_message(
                "No image-bearing manga folders were found in this working folder.\n\n"
                "Use Rescan after adding image folders."
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
        self._populate_folders(manga)

    def _on_manga_changed(self, _index: int) -> None:
        if not self._desktop_mutation_allowed(notify=False):
            return
        self._save_session()
        manga = self.manga_combo.currentData()
        if not isinstance(manga, MangaRef):
            return
        self._populate_folders(manga)

    def _populate_folders(self, manga: MangaRef) -> None:
        self._save_session()
        self.current_manga = manga
        self.current_folder = None
        self.current_image_index = 0
        self.selected_images.clear()
        self.editing_snapshot = None

        if self.editing_store is None:
            self._set_review_enabled(False)
            return
        try:
            snapshot = self.editing_store.load(manga)
        except OSError as exc:
            self.folder_combo.clear()
            self.folder_combo.setEnabled(False)
            self.canvas.show_message("Editing state for this manga could not be loaded safely.")
            self.heading_label.setText(manga.name)
            self.image_name_label.setText(str(exc))
            self._set_review_enabled(False)
            QMessageBox.critical(
                self,
                "Editing state needs attention",
                f"{exc}\n\nExport and completion remain disabled for this manga so "
                "saved output ownership is not lost.",
            )
            return

        self.editing_snapshot = snapshot
        target_name = snapshot.last_folder
        self.folder_combo.blockSignals(True)
        self.folder_combo.clear()
        for folder in manga.folders:
            self.folder_combo.addItem(folder.name, folder)
            self.folder_combo.setItemData(
                self.folder_combo.count() - 1,
                f"{len(folder.images)} images",
                Qt.ItemDataRole.ToolTipRole,
            )

        preferred_index = 0
        for index, folder in enumerate(manga.folders):
            if target_name and folder.name == target_name:
                preferred_index = index
                break
        self.folder_combo.setCurrentIndex(preferred_index)
        self.folder_combo.blockSignals(False)
        self.folder_combo.setEnabled(bool(manga.folders))
        self._load_folder(
            self.folder_combo.currentData(),
            snapshot=snapshot,
            resume_saved_position=(target_name == snapshot.last_folder),
        )

    def _on_folder_changed(self, _index: int) -> None:
        if not self._desktop_mutation_allowed(notify=False):
            return
        self._load_folder(self.folder_combo.currentData())

    def _load_folder(
        self,
        folder: object,
        *,
        snapshot: EditingSnapshot | None = None,
        resume_saved_position: bool = False,
    ) -> None:
        self._save_session()
        manga = self.current_manga
        if (
            not isinstance(folder, FolderRef)
            or manga is None
            or self.editing_store is None
        ):
            self.current_folder = None
            self._set_review_enabled(False)
            return

        if snapshot is None:
            try:
                snapshot = self.editing_store.load(manga)
            except OSError as exc:
                self.current_folder = None
                self._set_review_enabled(False)
                QMessageBox.critical(self, "Editing state needs attention", str(exc))
                return

        self.current_folder = folder
        self.editing_snapshot = snapshot
        self.canvas.clear_cache()
        folder_state = snapshot.folders.get(folder.name)
        image_names = {image.name for image in folder.images}
        current_name = (
            snapshot.last_image
            if resume_saved_position and snapshot.last_folder == folder.name
            else ""
        )
        self.current_image_index = next(
            (
                index
                for index, image in enumerate(folder.images)
                if image.name == current_name
            ),
            0,
        )
        self.selected_images = (
            set(folder_state.selected_images) & image_names
            if folder_state is not None
            else set()
        )
        try:
            self._last_output_directory = manga_output_directory(
                self.working_directory, manga
            )
        except OSError as exc:
            # Output ownership is irrelevant to position/selection editing.
            # Keep review available and let Export/Complete report their
            # stricter managed-output validation when invoked.
            self._last_output_directory = None
            self.statusBar().showMessage(
                f"Output is unavailable for this manga: {exc}", 10000
            )
        self._set_save_status(False, "Progress autosaves")
        self.settings.setValue("library/last_manga", manga.name)
        self._set_review_enabled(True)
        self._show_current_image()
        self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)

        if snapshot.warnings:
            self.statusBar().showMessage(" ".join(snapshot.warnings), 10000)

    def _show_current_image(self) -> None:
        manga = self.current_manga
        folder = self.current_folder
        if manga is None or folder is None or not folder.images:
            return

        self.current_image_index = min(
            max(self.current_image_index, 0), len(folder.images) - 1
        )
        image = folder.images[self.current_image_index]
        display_error = self.canvas.show_image(image.path)
        self.canvas.set_selected(image.name in self.selected_images)
        self.heading_label.setText(f"{manga.name}  ·  {folder.name}")
        self.progress_label.setText(
            f"{self.current_image_index + 1} / {len(folder.images)}"
        )
        self.image_name_label.setText(image.name)
        self.image_name_label.setToolTip(str(image.path))
        self._refresh_selection_controls()
        QTimer.singleShot(50, self._preload_neighbor_images)

        if display_error:
            self.statusBar().showMessage(
                f"Could not display {folder.name}/{image.name}: {display_error}", 8000
            )

    def _preload_neighbor_images(self) -> None:
        folder = self.current_folder
        if folder is None:
            return
        neighbor_paths = tuple(
            folder.images[index].path
            for index in (
                self.current_image_index + 1,
                self.current_image_index - 1,
            )
            if 0 <= index < len(folder.images)
        )
        self.canvas.preload_images(neighbor_paths)

    def _refresh_selection_controls(self) -> None:
        folder = self.current_folder
        if folder is None or not folder.images:
            return

        image = folder.images[self.current_image_index]
        is_selected = image.name in self.selected_images
        folder_selected_count = len(self.selected_images)
        other_selected_count = 0
        has_prior_exports = False
        if self.editing_snapshot is not None:
            other_selected_count = sum(
                len(state.selected_images)
                for name, state in self.editing_snapshot.folders.items()
                if name != folder.name
            )
            has_prior_exports = bool(self.editing_snapshot.exports)
        manga_selected_count = folder_selected_count + other_selected_count
        desktop_enabled = self._desktop_mutation_allowed(notify=False)
        self.canvas.set_selected(is_selected)
        self.toggle_button.setText(
            "Deselect Image" if is_selected else "Select Image"
        )
        self.selection_label.setText(
            (
                f"✓ {folder_selected_count} in folder · {manga_selected_count} total"
                if manga_selected_count
                else "0 selected"
            )
        )
        self.export_button.setText(
            f"Export Manga · {manga_selected_count} Selected"
            if manga_selected_count
            else "Synchronize Manga Output"
        )
        can_export = desktop_enabled and (manga_selected_count > 0 or has_prior_exports)
        self.export_button.setEnabled(can_export)
        self.export_action.setEnabled(can_export)
        self.clear_action.setEnabled(desktop_enabled and folder_selected_count > 0)
        self.previous_selected_action.setEnabled(
            desktop_enabled and folder_selected_count > 0
        )
        self.next_selected_action.setEnabled(
            desktop_enabled and folder_selected_count > 0
        )
        self.next_selected_button.setEnabled(
            desktop_enabled and folder_selected_count > 0
        )

    def navigate(self, offset: int) -> None:
        if not self._desktop_mutation_allowed():
            return
        folder = self.current_folder
        if folder is None:
            return
        target = self.current_image_index + offset
        if target < 0:
            self.statusBar().showMessage("Already at the first image.", 2500)
            return
        if target >= len(folder.images):
            self.statusBar().showMessage("Already at the last image.", 2500)
            return
        self.current_image_index = target
        self._schedule_session_save()
        self._show_current_image()

    def go_to_index(self, index: int) -> None:
        if not self._desktop_mutation_allowed():
            return
        folder = self.current_folder
        if folder is None:
            return
        target = min(max(index, 0), len(folder.images) - 1)
        if target == self.current_image_index:
            self.statusBar().showMessage(
                "Already at the first image." if target == 0 else "Already on this image.",
                2500,
            )
            return
        self.current_image_index = target
        self._schedule_session_save()
        self._show_current_image()

    def go_to_last_image(self) -> None:
        if not self._desktop_mutation_allowed():
            return
        if self.current_folder is not None:
            self.go_to_index(len(self.current_folder.images) - 1)

    def navigate_selected(self, direction: int) -> None:
        """Jump among selected images in the current folder, wrapping at either end."""

        if not self._desktop_mutation_allowed():
            return

        folder = self.current_folder
        if folder is None or not self.selected_images:
            return

        selected_indices = [
            index
            for index, image in enumerate(folder.images)
            if image.name in self.selected_images
        ]
        if not selected_indices:
            return

        if direction < 0:
            candidates = [
                index for index in selected_indices if index < self.current_image_index
            ]
            target = candidates[-1] if candidates else selected_indices[-1]
            wrapped = not candidates
        else:
            candidates = [
                index for index in selected_indices if index > self.current_image_index
            ]
            target = candidates[0] if candidates else selected_indices[0]
            wrapped = not candidates

        self.current_image_index = target
        self._schedule_session_save()
        self._show_current_image()
        if wrapped and len(selected_indices) > 1:
            edge = "last" if direction < 0 else "first"
            self.statusBar().showMessage(f"Wrapped to the {edge} selected image.", 2500)

    def toggle_current_selection(self) -> None:
        if not self._desktop_mutation_allowed():
            return
        folder = self.current_folder
        if folder is None:
            return
        image = folder.images[self.current_image_index]
        if image.name in self.selected_images:
            self.selected_images.remove(image.name)
            message = f"Deselected {image.name}."
        else:
            self.selected_images.add(image.name)
            message = f"Selected {image.name}."
        self._save_session()
        self._refresh_selection_controls()
        self.statusBar().showMessage(message, 2000)

    def select_current_and_advance(self) -> None:
        if not self._desktop_mutation_allowed():
            return
        folder = self.current_folder
        if folder is None:
            return
        image = folder.images[self.current_image_index]
        self.selected_images.add(image.name)
        if self.current_image_index < len(folder.images) - 1:
            self.current_image_index += 1
            self._save_session()
            self._show_current_image()
        else:
            self._save_session()
            self._refresh_selection_controls()
            self.statusBar().showMessage("Selected the final image in this folder.", 3000)

    def clear_selections(self) -> None:
        if not self._desktop_mutation_allowed():
            return
        if not self.selected_images or self.current_folder is None:
            return
        answer = QMessageBox.question(
            self,
            "Clear selections",
            f"Clear all {len(self.selected_images)} selections in "
            f"{self.current_folder.name}?\n\n"
            "Previously exported files will be removed only after the next manga export.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.selected_images.clear()
            self._save_session()
            self._refresh_selection_controls()
            self.statusBar().showMessage("Selections cleared.", 3000)

    def export_selection(self) -> None:
        if not self._desktop_mutation_allowed():
            return
        manga = self.current_manga
        if (
            manga is None
            or self.current_folder is None
            or self.working_directory is None
            or self.editing_store is None
        ):
            return

        if not self._save_session() or self._pending_session_saves:
            QMessageBox.warning(
                self,
                "Progress has not finished saving",
                "Export was not started because the latest editing state could not "
                "be saved safely. Wait for the active library operation and try again.",
            )
            return
        try:
            snapshot = self.editing_store.load(manga)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        selected_count = sum(
            len(folder_state.selected_images)
            for folder_state in snapshot.folders.values()
        )
        affected_folders = {
            name
            for name, folder_state in snapshot.folders.items()
            if folder_state.selected_images
        } | set(snapshot.exports)
        if not selected_count and not snapshot.exports:
            QMessageBox.warning(
                self, "Nothing to export", "Select at least one image before exporting."
            )
            return

        try:
            destination = manga_output_directory(self.working_directory, manga)
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Export failed",
                f"The manga output path is not safe to use:\n\n{exc}",
            )
            self.statusBar().showMessage(f"Export failed: {exc}", 10000)
            return
        answer = QMessageBox.question(
            self,
            "Synchronize manga output",
            f"Synchronize {selected_count} selected image(s) across "
            f"{len(affected_folders)} folder(s) to:\n\n{destination}\n\n"
            "The output will exactly match current selections: newly selected images "
            "are added and stale app-managed images are removed. Untracked files are preserved.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            result = export_manga(self.working_directory, manga)
        except ExportBusyError as exc:
            QMessageBox.warning(
                self,
                "Library busy",
                f"{exc}\n\nNo export was started. Try again after the active "
                "library operation finishes.",
            )
            self.statusBar().showMessage("Library busy; export was not started.", 8000)
            return
        except ExportRecoveryError as exc:
            # An incomplete rollback can leave output/editing.json in either
            # their active or journal locations. Detach before recovery so an
            # autosave cannot alter the evidence needed to reconcile them.
            self._session_save_timer.stop()
            self._discard_pending_session_saves(manga)
            self.current_manga = None
            self.current_folder = None
            self.current_image_index = 0
            self.selected_images.clear()
            self.editing_snapshot = None
            self._last_output_directory = None
            self.canvas.clear_cache()
            rescanned, _recovery = self._rescan(show_recovery_dialog=False)
            follow_up = (
                "Automatic recovery completed and the library was reloaded. Review "
                "the manga output before exporting again."
                if rescanned
                else "The library remains detached. Resolve the reported recovery "
                "problem, then use Rescan."
            )
            QMessageBox.critical(
                self,
                "Export needs attention",
                f"{exc}\n\n{follow_up}",
            )
            self.statusBar().showMessage(
                "Export rollback was incomplete; review the recovered library.", 15000
            )
            return
        except ExportError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            self.statusBar().showMessage(f"Export failed: {exc}", 10000)
            return

        export_message = (
            f"Synchronized manga output at:\n\n{result.output_directory}\n\n"
            f"Added or refreshed: {result.copied_count}\n"
            f"Retained unchanged: {result.retained_count}\n"
            f"Removed stale: {result.removed_count}"
        )
        if result.warnings:
            # A committed transaction may still own cleanup payload. Recover
            # it before any position autosave can change editing.json's digest.
            self._session_save_timer.stop()
            self._discard_pending_session_saves(manga)
            self.current_manga = None
            self.current_folder = None
            self.current_image_index = 0
            self.selected_images.clear()
            self.editing_snapshot = None
            self._last_output_directory = None
            self.canvas.clear_cache()
            rescanned, _recovery = self._rescan(show_recovery_dialog=False)
            export_message += (
                "\n\nThe export was committed, but temporary cleanup needs "
                "attention:\n• " + "\n• ".join(result.warnings)
            )
            export_message += (
                "\n\nAutomatic recovery completed and the library was reloaded."
                if rescanned
                else "\n\nFollow-up recovery failed, so the library remains detached. "
                "Resolve the reported problem and use Rescan."
            )
            QMessageBox.warning(
                self, "Export complete with a warning", export_message
            )
        else:
            self._last_output_directory = result.output_directory
            try:
                self.editing_snapshot = self.editing_store.load(manga)
            except OSError as exc:
                self._session_save_timer.stop()
                self._discard_pending_session_saves(manga)
                self.current_manga = None
                self.current_folder = None
                self.current_image_index = 0
                self.selected_images.clear()
                self.editing_snapshot = None
                self._last_output_directory = None
                self.canvas.clear_cache()
                rescanned, _recovery = self._rescan(show_recovery_dialog=False)
                QMessageBox.warning(
                    self,
                    "Export complete; reload needed",
                    f"{export_message}\n\nThe export committed, but editing state "
                    f"could not be reloaded:\n{exc}\n\n"
                    + (
                        "The library was recovered and reloaded; review it before continuing."
                        if rescanned
                        else "The library remains detached. Resolve the reported problem, "
                        "then use Rescan."
                    ),
                )
                self.statusBar().showMessage(
                    "Export committed, but editing state needs to be reloaded.", 10000
                )
                return
            self.open_output_button.setEnabled(result.output_directory.is_dir())
            QMessageBox.information(self, "Export complete", export_message)
        self.statusBar().showMessage(
            (
                "Export committed, but temporary cleanup needs attention."
                if result.warnings
                else f"Synchronized {selected_count} selected image(s) across the manga."
            ),
            10000 if result.warnings else 5000,
        )
        if self.current_folder is not None:
            self._refresh_selection_controls()
            self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)

    def open_output_directory(self) -> None:
        if not self._desktop_mutation_allowed():
            return
        if self._last_output_directory is None or not self._last_output_directory.is_dir():
            return
        opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_output_directory)))
        if not opened:
            QMessageBox.warning(
                self,
                "Could not open output folder",
                f"Open this folder manually:\n\n{self._last_output_directory}",
            )

    def complete_current_manga(self) -> None:
        """Finalize one manga after explicit destructive confirmation."""

        if not self._desktop_mutation_allowed():
            return

        manga = self.manga_combo.currentData()
        if not isinstance(manga, MangaRef) or self.working_directory is None:
            return

        # Flush any pending review change so all of this manga's state can be
        # removed as one completion batch.
        if not self._save_session() or self._pending_session_saves:
            QMessageBox.warning(
                self,
                "Progress has not finished saving",
                "Completion was not started because the latest editing state could "
                "not be saved safely. Wait for the active library operation and try again.",
            )
            return
        try:
            preview = analyze_completion(self.working_directory, manga)
        except CompletionError as exc:
            QMessageBox.warning(self, "Cannot complete manga", str(exc))
            self.statusBar().showMessage(f"Cannot complete manga: {exc}", 10000)
            return

        prior_batch_text = (
            f"{len(preview.existing_batches)} earlier completed batch(es) will remain unchanged."
            if preview.existing_batches
            else "This will be the first completed batch for this manga."
        )
        answer = QMessageBox.warning(
            self,
            "Permanently complete manga",
            f"Complete {manga.name}?\n\n"
            f"Source image folders: {preview.source_folder_count}\n"
            f"Exported folders: {preview.exported_folder_count}\n"
            f"Exported images: {preview.total_image_count}\n"
            f"Destination batch: {preview.destination_batch.name}\n\n"
            f"{prior_batch_text}\n\n"
            "Only images already in Output will be kept; current selections are not "
            "exported automatically. The entire current output will become the new "
            "immutable completed batch. Reading progress, editing selections, and "
            "export bookkeeping for this active source will be removed.\n\n"
            f"The source folder will be permanently deleted:\n{preview.source_directory}\n\n"
            "This cannot be undone through Pocket Manga Editor.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self._session_save_timer.stop()
        try:
            result = complete_manga(self.working_directory, manga, preview)
        except CompletionBusyError as exc:
            self._flush_pending_session_saves()
            QMessageBox.warning(
                self,
                "Library busy",
                f"{exc}\n\nNo files were changed by this completion attempt. "
                "Try again after the other library operation finishes.",
            )
            self.statusBar().showMessage("Library busy; completion was not started.", 8000)
            return
        except CompletionRecoveryError as exc:
            self._discard_pending_session_saves(manga)
            rescanned, recovery = self._detach_completed_manga_and_rescan(
                show_recovery_dialog=False
            )
            if not rescanned:
                return
            recovery_text = _completion_recovery_result_text(recovery)
            QMessageBox.critical(
                self,
                "Completion needs attention",
                f"{exc}\n\nThe library was rescanned because some files may have "
                f"moved.{recovery_text}\n\nReview the source, output, completed folder, and any "
                "transaction folder reported by the error before trying again.",
            )
            self.statusBar().showMessage(
                "Completion could not be rolled back completely; review the library.",
                15000,
            )
            return
        except CompletionError as exc:
            self._flush_pending_session_saves()
            QMessageBox.critical(self, "Completion failed", str(exc))
            self.statusBar().showMessage(f"Completion failed: {exc}", 10000)
            return

        self._discard_pending_session_saves(manga)
        rescanned, recovery = self._detach_completed_manga_and_rescan(
            show_recovery_dialog=False
        )

        cleanup_text = ""
        if result.cleanup_warnings:
            cleanup_text = (
                "\n\nThe completion was committed, but some temporary cleanup "
                "needs attention:\n• "
                + "\n• ".join(result.cleanup_warnings)
            )
        if not rescanned:
            cleanup_text += (
                "\n\nThe completion is committed, but the follow-up library scan "
                "failed. Close other app windows and use Rescan before continuing."
            )
        recovery_text = _completion_recovery_result_text(recovery)
        message = (
            f"Completed {manga.name}.\n\n"
            f"Moved {result.total_image_count} image(s) to:\n"
            f"{result.batch_directory}\n\n"
            "The source manga and its active reading/editing state were removed. "
            f"Earlier completed batches were left unchanged.{cleanup_text}{recovery_text}"
        )
        has_warning = bool(result.cleanup_warnings) or not rescanned
        if has_warning:
            QMessageBox.warning(self, "Manga completed with a warning", message)
        else:
            QMessageBox.information(self, "Manga completed", message)
        status_text = (
            f"Completed {manga.name}, but the library needs attention."
            if has_warning
            else f"Completed {manga.name} with {result.total_image_count} image(s)."
        )
        self.statusBar().showMessage(status_text, 15000 if has_warning else 8000)

    def _detach_completed_manga_and_rescan(
        self, *, show_recovery_dialog: bool
    ) -> tuple[bool, CompletionRecoveryResult | None]:
        """Prevent pending saves from recreating state after destructive moves."""

        self._session_save_timer.stop()
        self.current_manga = None
        self.current_folder = None
        self.current_image_index = 0
        self.selected_images.clear()
        self.editing_snapshot = None
        self._last_output_directory = None
        self.canvas.clear_cache()
        self.settings.remove("library/last_manga")
        return self._rescan(show_recovery_dialog=show_recovery_dialog)

    def _schedule_session_save(self) -> None:
        if not self._desktop_mutation_allowed(notify=False):
            self._session_save_timer.stop()
            return
        self._set_save_status(False, "Saving progress…")
        self._session_save_timer.start(300)

    def _save_session(self) -> bool:
        self._session_save_timer.stop()
        if not self._desktop_mutation_allowed(notify=False):
            return not self._pending_session_saves
        if (
            self.current_manga is not None
            and self.current_folder is not None
            and self.editing_store is not None
            and self.current_folder.images
        ):
            store = self.editing_store
            manga = self.current_manga
            folder = self.current_folder
            index = min(
                max(self.current_image_index, 0), len(folder.images) - 1
            )
            current_image = folder.images[index].name
            key = (
                str(store.working_directory.resolve()),
                manga.name,
                folder.name,
            )
            self._pending_session_saves[key] = (
                store,
                manga,
                folder,
                current_image,
                frozenset(self.selected_images),
            )
        return self._flush_pending_session_saves()

    def _flush_pending_session_saves(self) -> bool:
        """Persist queued immutable snapshots without retargeting a busy retry."""

        last_error: Exception | None = None
        while self._pending_session_saves:
            key = next(iter(self._pending_session_saves))
            store, manga, folder, current_image, selected_images = (
                self._pending_session_saves[key]
            )
            try:
                snapshot = store.save_folder(
                    manga,
                    folder.name,
                    current_image,
                    selected_images,
                )
            except LibraryBusyError as exc:
                self._set_save_status(False, "Waiting to save…")
                self.save_status_label.setToolTip(str(exc))
                self._session_save_timer.start(750)
                return False
            except (EditingStateError, OSError) as exc:
                last_error = exc
                self._pending_session_saves.pop(key)
            else:
                self._pending_session_saves.pop(key)
                if self.current_manga is not None and self.current_manga.path == manga.path:
                    self.editing_snapshot = snapshot

        if last_error is not None:
            self._set_save_status(True, "⚠ Progress not saved")
            self.save_status_label.setToolTip(str(last_error))
            self.statusBar().showMessage(
                f"Could not save review progress: {last_error}", 10000
            )
        else:
            self._set_save_status(False, "✓ Progress saved")
            self.save_status_label.setToolTip("")

        if self._close_requested:
            self._close_requested = False
            QTimer.singleShot(0, self.close)
        return last_error is None

    def _discard_pending_session_saves(self, manga: MangaRef) -> None:
        """Drop snapshots whose source was just permanently completed."""

        for key, (_store, saved_manga, _folder, _image, _selected) in tuple(
            self._pending_session_saves.items()
        ):
            if saved_manga.path == manga.path:
                self._pending_session_saves.pop(key)

    def _set_save_status(self, error: bool, text: str) -> None:
        self.save_status_label.setText(text)
        self.save_status_label.setProperty("error", error)
        self.save_status_label.style().unpolish(self.save_status_label)
        self.save_status_label.style().polish(self.save_status_label)

    def _set_review_enabled(self, enabled: bool) -> None:
        enabled = enabled and self._desktop_mutation_allowed(notify=False)
        for action in (
            self.previous_action,
            self.next_action,
            self.previous_selected_action,
            self.next_selected_action,
            self.toggle_action,
            self.select_next_action,
            self.first_action,
            self.last_action,
            self.complete_action,
        ):
            action.setEnabled(enabled)

        self.previous_button.setEnabled(enabled)
        self.next_button.setEnabled(enabled)
        self.next_selected_button.setEnabled(enabled and bool(self.selected_images))
        self.toggle_button.setEnabled(enabled)
        self.complete_button.setEnabled(enabled)
        has_folder_selection = enabled and bool(self.selected_images)
        total_selection_count = len(self.selected_images)
        has_prior_exports = False
        if self.editing_snapshot is not None:
            total_selection_count += sum(
                len(state.selected_images)
                for name, state in self.editing_snapshot.folders.items()
                if self.current_folder is None or name != self.current_folder.name
            )
            has_prior_exports = bool(self.editing_snapshot.exports)
        can_export = enabled and (total_selection_count > 0 or has_prior_exports)
        self.export_action.setEnabled(can_export)
        self.export_button.setEnabled(can_export)
        self.clear_action.setEnabled(has_folder_selection)
        self.open_output_button.setEnabled(
            enabled
            and self._last_output_directory is not None
            and self._last_output_directory.is_dir()
        )

    def _clear_review_display(self, message: str) -> None:
        self.manga_combo.blockSignals(True)
        self.folder_combo.blockSignals(True)
        self.manga_combo.clear()
        self.folder_combo.clear()
        self.manga_combo.blockSignals(False)
        self.folder_combo.blockSignals(False)
        self.manga_combo.setEnabled(False)
        self.folder_combo.setEnabled(False)
        self.heading_label.setText("No image folder open")
        self.progress_label.setText("— / —")
        self.image_name_label.setText("")
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
            "Unsafe, unreadable, or unsupported source items were skipped."
        )
        dialog.setDetailedText(_format_scan_issues(self.scan_result.issues))
        dialog.exec()

    def show_keyboard_help(self) -> None:
        QMessageBox.information(
            self,
            "Keyboard shortcuts",
            "<h3>Review controls</h3>"
            "<table cellspacing='8'>"
            "<tr><td><b>← / →</b></td><td>Previous or next image</td></tr>"
            "<tr><td><b>Ctrl+← / Ctrl+→</b></td><td>Previous or next selected image</td></tr>"
            "<tr><td><b>Space</b></td><td>Select or deselect the current image</td></tr>"
            "<tr><td><b>Enter</b></td><td>Select the current image and advance</td></tr>"
            "<tr><td><b>Home / End</b></td><td>Go to the first or last image</td></tr>"
            "<tr><td><b>Ctrl+S</b></td><td>Synchronize all manga selections to output</td></tr>"
            "<tr><td><b>F5</b></td><td>Rescan the working folder</td></tr>"
            "<tr><td><b>? / F1</b></td><td>Show this help</td></tr>"
            "</table>"
            "<p>Editing selections and the current folder/image are saved automatically. "
            "One export synchronizes every selected image folder in the manga. Complete "
            "Manga permanently deletes the source folder after destructive confirmation.</p>"
            "<p>Companion Mode transfers review ownership to one paired phone on "
            "the same trusted network for either independent reading or shared editing. "
            "Desktop mutation controls remain locked until Companion Mode ends.</p>",
        )

    def show_about(self) -> None:
        QMessageBox.about(
            self,
            "About Pocket Manga Editor",
            "<h3>Pocket Manga Editor</h3>"
            "<p>A filesystem-faithful tool for reading on a local-network phone, "
            "editing selections on phone or desktop, and synchronizing manga output.</p>"
            "<p>Development version 0.2.0</p>",
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API name
        coordinator = self.companion_coordinator
        if coordinator is not None:
            state = coordinator.status().state
            try:
                if state is CompanionState.COMPANION_ACTIVE:
                    context = coordinator.begin_exit()
                    self._remember_mobile_context(context)
                    self.current_manga = None
                    self.current_folder = None
                    self.current_image_index = 0
                    self.selected_images.clear()
                    self.editing_snapshot = None
                    coordinator.finish_exit()
                elif state is CompanionState.EXITING_COMPANION:
                    self._remember_mobile_context(
                        self._pending_mobile_context
                        or coordinator.status().mobile_context
                    )
                    self.current_manga = None
                    self.current_folder = None
                    self.current_image_index = 0
                    self.selected_images.clear()
                    self.editing_snapshot = None
                    coordinator.finish_exit()
                elif state is CompanionState.COMPANION_ERROR:
                    answer = QMessageBox.warning(
                        self,
                        "Companion recovery required",
                        "Companion writes are blocked after an error. Recover desktop "
                        "ownership and close the application?",
                        QMessageBox.StandardButton.Yes
                        | QMessageBox.StandardButton.Cancel,
                        QMessageBox.StandardButton.Cancel,
                    )
                    if answer != QMessageBox.StandardButton.Yes:
                        event.ignore()
                        return
                    context = coordinator.begin_recovery()
                    self._remember_mobile_context(context)
                    self.current_manga = None
                    self.current_folder = None
                    self.current_image_index = 0
                    self.selected_images.clear()
                    self.editing_snapshot = None
                    rescanned, _recovery = self._rescan(
                        show_recovery_dialog=False
                    )
                    if not rescanned:
                        event.ignore()
                        return
                    coordinator.finish_recovery()
            except Exception as exc:
                QMessageBox.critical(
                    self,
                    "Could not close safely",
                    "Companion review state could not be drained, so the window will "
                    f"remain open.\n\n{exc}",
                )
                event.ignore()
                return

        self._save_session()
        if self._pending_session_saves:
            self._close_requested = True
            self.statusBar().showMessage(
                "Waiting to save review progress before closing…"
            )
            event.ignore()
            return

        self._close_requested = False
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/main_splitter", self.main_splitter.saveState())
        self.settings.sync()
        event.accept()


def _completion_recovery_result_text(
    recovery: CompletionRecoveryResult | None,
) -> str:
    """Summarize a suppressed recovery dialog inside the caller's final message."""

    if recovery is None or not (recovery.recovered_count or recovery.warnings):
        return ""
    details: list[str] = []
    if recovery.rolled_back_count:
        details.append(f"restored {recovery.rolled_back_count} interrupted batch(es)")
    if recovery.cleaned_count:
        details.append(f"finished cleanup for {recovery.cleaned_count} batch(es)")
    details.extend(recovery.warnings)
    return "\n\nAutomatic recovery: " + "; ".join(details) + "."


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
