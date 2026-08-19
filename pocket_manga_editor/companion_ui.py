"""Qt widgets for the desktop side of Companion Mode."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class CompanionConnectionDialog(QDialog):
    """Edit the stable LAN address advertised to the Home Screen app."""

    def __init__(
        self,
        *,
        public_host: str,
        port: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Companion connection settings")
        self.setModal(True)
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        explanation = QLabel(
            "Use an IP address reserved for this PC by your router so the iPhone "
            "Home Screen address stays stable. Companion Mode uses local HTTP and "
            "should only be enabled on a trusted home network."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        form = QFormLayout()
        self.host_edit = QLineEdit(public_host)
        self.host_edit.setPlaceholderText("For example: 192.168.1.20")
        self.host_edit.setClearButtonEnabled(True)
        form.addRow("Reserved PC address", self.host_edit)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(port)
        form.addRow("Fixed port", self.port_spin)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[str | None, int]:
        host = self.host_edit.text().strip()
        return (host or None, self.port_spin.value())


class CompanionStatusPanel(QFrame):
    """Read-only ownership status shown while the phone controls review state."""

    pair_requested = Signal()
    copy_url_requested = Signal()
    disconnect_requested = Signal()
    forget_requested = Signal()
    retry_server_requested = Signal()
    end_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("companionStatusPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 36, 36, 36)
        outer.setSpacing(18)
        outer.addStretch(1)

        card = QFrame()
        card.setObjectName("companionStatusCard")
        card.setMaximumWidth(720)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(16)

        eyebrow = QLabel("COMPANION MODE")
        eyebrow.setObjectName("companionEyebrow")
        layout.addWidget(eyebrow)

        self.title_label = QLabel("Review ownership is on your phone")
        self.title_label.setObjectName("companionTitle")
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        self.explanation_label = QLabel(
            "Desktop review, export, completion, rescan, and folder changes stay "
            "locked until Companion Mode ends."
        )
        self.explanation_label.setObjectName("mutedLabel")
        self.explanation_label.setWordWrap(True)
        layout.addWidget(self.explanation_label)

        self.server_error_label = QLabel()
        self.server_error_label.setObjectName("companionError")
        self.server_error_label.setWordWrap(True)
        self.server_error_label.hide()
        layout.addWidget(self.server_error_label)

        details = QGridLayout()
        details.setHorizontalSpacing(16)
        details.setVerticalSpacing(12)
        details.setColumnStretch(1, 1)
        _, self.url_label = self._add_detail(details, 0, "Mobile address", "Unavailable")
        _, self.pairing_label = self._add_detail(details, 1, "Paired device", "None")
        _, self.client_label = self._add_detail(details, 2, "Active controller", "None")
        _, self.context_label = self._add_detail(details, 3, "Phone position", "Waiting")
        self.selection_title_label, self.selection_label = self._add_detail(
            details, 4, "Selected images", "—"
        )
        _, self.code_label = self._add_detail(details, 5, "Pairing code", "Not open")
        self.url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addLayout(details)

        connection_buttons = QHBoxLayout()
        connection_buttons.setSpacing(8)
        self.copy_url_button = QPushButton("Copy URL")
        self.copy_url_button.clicked.connect(self.copy_url_requested.emit)
        connection_buttons.addWidget(self.copy_url_button)
        self.pair_button = QPushButton("Create Pairing Code")
        self.pair_button.clicked.connect(self.pair_requested.emit)
        connection_buttons.addWidget(self.pair_button)
        self.retry_button = QPushButton("Retry Server")
        self.retry_button.clicked.connect(self.retry_server_requested.emit)
        self.retry_button.hide()
        connection_buttons.addWidget(self.retry_button)
        connection_buttons.addStretch(1)
        layout.addLayout(connection_buttons)

        session_buttons = QHBoxLayout()
        session_buttons.setSpacing(8)
        self.disconnect_button = QPushButton("Disconnect Mobile Client")
        self.disconnect_button.clicked.connect(self.disconnect_requested.emit)
        session_buttons.addWidget(self.disconnect_button)
        self.forget_button = QPushButton("Forget Paired Device")
        self.forget_button.clicked.connect(self.forget_requested.emit)
        session_buttons.addWidget(self.forget_button)
        session_buttons.addStretch(1)
        layout.addLayout(session_buttons)

        self.end_button = QPushButton("End Companion Mode")
        self.end_button.setObjectName("primaryButton")
        self.end_button.clicked.connect(self.end_requested.emit)
        layout.addWidget(self.end_button)

        centered = QHBoxLayout()
        centered.addStretch(1)
        centered.addWidget(card, 1)
        centered.addStretch(1)
        outer.addLayout(centered)
        outer.addStretch(1)

    @staticmethod
    def _add_detail(
        layout: QGridLayout, row: int, title: str, value: str
    ) -> tuple[QLabel, QLabel]:
        title_label = QLabel(title)
        title_label.setObjectName("fieldLabel")
        title_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        value_label = QLabel(value)
        value_label.setObjectName("companionDetail")
        value_label.setWordWrap(True)
        layout.addWidget(title_label, row, 0)
        layout.addWidget(value_label, row, 1)
        return title_label, value_label

    def update_status(
        self,
        *,
        url: str | None,
        paired: bool,
        pairing_code: str | None,
        pairing_expires_text: str | None,
        client_name: str | None,
        context: str | None,
        selected_count: int | None,
        server_error: str | None,
        server_retry_available: bool,
    ) -> None:
        """Refresh labels from a thread-safe coordinator status snapshot."""

        self.url_label.setText(url or "Unavailable")
        self.copy_url_button.setEnabled(bool(url))
        self.pairing_label.setText("Authorized" if paired else "None")
        self.client_label.setText(client_name or "None connected")
        self.context_label.setText(context or "Waiting for the phone")
        self.selection_label.setText(
            str(selected_count) if selected_count is not None else "—"
        )
        self.selection_title_label.setVisible(selected_count is not None)
        self.selection_label.setVisible(selected_count is not None)
        code_text = pairing_code or "Not open"
        if pairing_code and pairing_expires_text:
            code_text += f"  ·  expires {pairing_expires_text}"
        self.code_label.setText(code_text)

        self.disconnect_button.setEnabled(bool(client_name))
        self.forget_button.setEnabled(paired)
        self.pair_button.setEnabled(not paired)
        self.server_error_label.setText(server_error or "")
        self.server_error_label.setVisible(bool(server_error))
        self.retry_button.setVisible(bool(server_error) and server_retry_available)

    def update_mode(self, title: str, explanation: str) -> None:
        """Describe the current ownership/transition state without ambiguity."""

        self.title_label.setText(title)
        self.explanation_label.setText(explanation)
