"""Dark theme stylesheet for the streaming demo."""

DARK_STYLE = """
QMainWindow, QWidget { background-color: #0c0c0c; color: #ccc; }
QScrollArea { border: none; background-color: #111; }

QGroupBox {
    background-color: #111; color: #00e87a;
    font: bold 8pt "Courier New";
    border: 1px solid #222; border-radius: 4px;
    margin-top: 10px; padding-top: 14px;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 10px; padding: 0 4px;
}

QLabel { color: #aaa; font: 9pt "Courier New"; }

QPushButton {
    background-color: #1e1e1e; color: #ccc;
    font: bold 9pt "Courier New";
    border: none; padding: 6px 10px; border-radius: 3px;
}
QPushButton:hover { background-color: #333; }
QPushButton:pressed { background-color: #444; }
QPushButton:disabled { color: #555; background-color: #181818; }

QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {
    background-color: #1e1e1e; color: #00e87a;
    font: 10pt "Courier New";
    border: 1px solid #333; border-radius: 3px; padding: 3px;
    selection-background-color: #1a3a1a;
}
QComboBox QAbstractItemView { background-color: #1e1e1e; color: #00e87a; }

QSlider::groove:horizontal {
    background: #2a2a2a; height: 6px; border-radius: 3px;
}
QSlider::handle:horizontal {
    background: #00e87a; width: 14px; margin: -4px 0; border-radius: 7px;
}
QSlider::handle:horizontal:disabled { background: #444; }

QCheckBox, QRadioButton {
    color: #aaa; font: 9pt "Courier New"; spacing: 6px;
}
QCheckBox::indicator, QRadioButton::indicator {
    width: 14px; height: 14px;
}

QProgressBar {
    background-color: #1e1e1e; border: none; border-radius: 3px;
    text-align: center; color: #00e87a; font: 8pt "Courier New";
}
QProgressBar::chunk { background-color: #00e87a; border-radius: 3px; }

QSplitter::handle { background-color: #222; width: 3px; }
"""
