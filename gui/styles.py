"""Dark theme stylesheet for the application."""

DARK_STYLE = """
QMainWindow, QDialog {
    background-color: #1a1a2e;
    color: #e0e0e0;
}

QWidget {
    background-color: #1a1a2e;
    color: #e0e0e0;
    font-family: "Segoe UI", sans-serif;
    font-size: 13px;
}

QTabWidget::pane {
    border: 1px solid #2d2d44;
    background-color: #1a1a2e;
}

QTabBar::tab {
    background-color: #16213e;
    color: #a0a0c0;
    padding: 8px 20px;
    border: 1px solid #2d2d44;
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    min-width: 100px;
}

QTabBar::tab:selected {
    background-color: #1a1a2e;
    color: #ffffff;
    border-bottom: 2px solid #6c63ff;
}

QTabBar::tab:hover:!selected {
    background-color: #22224a;
    color: #d0d0f0;
}

QPushButton {
    background-color: #6c63ff;
    color: white;
    border: none;
    padding: 8px 20px;
    border-radius: 6px;
    font-weight: bold;
    font-size: 13px;
}

QPushButton:hover {
    background-color: #7c73ff;
}

QPushButton:pressed {
    background-color: #5c53ef;
}

QPushButton:disabled {
    background-color: #3a3a4a;
    color: #606070;
}

QPushButton#btn_danger:disabled,
QPushButton#btn_success:disabled,
QPushButton#btn_warning:disabled {
    background-color: #3a3a4a;
    color: #606070;
}

QPushButton#btn_danger {
    background-color: #e53935;
}
QPushButton#btn_danger:hover {
    background-color: #ef5350;
}

QPushButton#btn_success {
    background-color: #43a047;
}
QPushButton#btn_success:hover {
    background-color: #4caf50;
}

QPushButton#btn_warning {
    background-color: #fb8c00;
}
QPushButton#btn_warning:hover {
    background-color: #ffa726;
}

QComboBox {
    background-color: #16213e;
    color: #e0e0e0;
    border: 1px solid #3a3a5a;
    padding: 5px 10px;
    border-radius: 4px;
    min-height: 28px;
}

QComboBox::drop-down {
    border: none;
    width: 20px;
}

QComboBox QAbstractItemView {
    background-color: #16213e;
    color: #e0e0e0;
    selection-background-color: #6c63ff;
    border: 1px solid #3a3a5a;
}

QTableWidget {
    background-color: #16213e;
    color: #e0e0e0;
    gridline-color: #2a2a3e;
    border: 1px solid #2a2a3e;
    border-radius: 4px;
    selection-background-color: #3a3a6e;
}

QTableWidget::item {
    padding: 4px 8px;
    border-bottom: 1px solid #252535;
}

QTableWidget::item:selected {
    background-color: #3a3a6e;
}

QHeaderView::section {
    background-color: #0f3460;
    color: #c0c0e0;
    padding: 6px 8px;
    border: none;
    border-right: 1px solid #1a1a2e;
    font-weight: bold;
}

QLineEdit, QSpinBox {
    background-color: #16213e;
    color: #e0e0e0;
    border: 1px solid #3a3a5a;
    padding: 5px 10px;
    border-radius: 4px;
    min-height: 28px;
}

QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border: 1px solid #6c63ff;
}

QLabel {
    color: #e0e0e0;
    background: transparent;
}

QLabel#label_title {
    font-size: 18px;
    font-weight: bold;
    color: #ffffff;
}

QLabel#label_subtitle {
    font-size: 12px;
    color: #8080a0;
}

QLabel#label_value {
    font-size: 22px;
    font-weight: bold;
    color: #6c63ff;
}

QLabel#label_success {
    color: #4caf50;
    font-weight: bold;
}

QLabel#label_warning {
    color: #ff9800;
    font-weight: bold;
}

QLabel#label_error {
    color: #f44336;
    font-weight: bold;
}

QGroupBox {
    border: 1px solid #2d2d44;
    border-radius: 6px;
    margin-top: 14px;
    padding-top: 10px;
    color: #c0c0e0;
    font-weight: bold;
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
    color: #a0a0d0;
}

QScrollBar:vertical {
    background-color: #16213e;
    width: 10px;
    border-radius: 5px;
}

QScrollBar::handle:vertical {
    background-color: #3a3a6e;
    border-radius: 5px;
    min-height: 20px;
}

QScrollBar::handle:vertical:hover {
    background-color: #6c63ff;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

QProgressBar {
    background-color: #16213e;
    border: 1px solid #2a2a4e;
    border-radius: 6px;
    text-align: center;
    color: #ffffff;
    font-weight: bold;
    height: 18px;
}

QProgressBar::chunk {
    background-color: #6c63ff;
    border-radius: 5px;
}

QStatusBar {
    background-color: #0f3460;
    color: #a0a0c0;
    font-size: 12px;
}

QMenuBar {
    background-color: #16213e;
    color: #e0e0e0;
}

QMenuBar::item:selected {
    background-color: #6c63ff;
    border-radius: 4px;
}

QMenu {
    background-color: #16213e;
    color: #e0e0e0;
    border: 1px solid #3a3a5a;
}

QMenu::item:selected {
    background-color: #6c63ff;
}

QSplitter::handle {
    background-color: #2d2d44;
    width: 2px;
}

QFrame[frameShape="4"], QFrame[frameShape="5"] {
    color: #2d2d44;
}

QCheckBox {
    color: #e0e0e0;
    spacing: 6px;
}

QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #3a3a5a;
    border-radius: 3px;
    background-color: #16213e;
}

QCheckBox::indicator:checked {
    background-color: #6c63ff;
    border-color: #6c63ff;
}

QToolTip {
    background-color: #0f3460;
    color: #e0e0e0;
    border: 1px solid #3a3a5a;
    padding: 4px 8px;
    border-radius: 4px;
}
"""

# Color constants for programmatic use
COLORS = {
    "bg_deep":    "#1a1a2e",
    "bg_card":    "#16213e",
    "bg_header":  "#0f3460",
    "accent":     "#6c63ff",
    "success":    "#4caf50",
    "warning":    "#ff9800",
    "error":      "#f44336",
    "text":       "#e0e0e0",
    "text_muted": "#8080a0",
    "border":     "#2d2d44",
}
