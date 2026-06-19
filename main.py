"""IxxatInterface v7.0 — Entry point."""

import sys
import os

# Ensure imports resolve from project root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QFont, QIcon
from PyQt5.QtCore import Qt

from gui.app import MainWindow
from gui.styles import DARK_STYLE


def _get_icon_path() -> str:
    """Resolve o caminho do icon.ico — funciona em dev (.py) e PyInstaller (.exe)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", "icon.ico")


def main():
    # Enable high-DPI scaling
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("IxxatInterface")
    app.setApplicationVersion("7.0")
    app.setOrganizationName("INOVA Telemetria")
    app.setStyleSheet(DARK_STYLE)
    app.setFont(QFont("Segoe UI", 10))

    # Ícone global da aplicação (mostrado também na barra de tarefas)
    icon_path = _get_icon_path()
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # Windows: força a barra de tarefas a usar o ícone próprio (e não o do Python)
    if sys.platform == "win32":
        try:
            import ctypes
            myappid = "INOVA.IxxatInterface.7.0"
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except Exception:
            pass

    window = MainWindow()
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
