"""Shared desktop appearance and small, resolution-independent empty states."""

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QLabel, QSizePolicy, QStyle,
    QStyleOptionButton, QVBoxLayout, QWidget,
)


def short_path(value):
    path = Path(value).expanduser()
    try:
        return str(Path('~') / path.relative_to(Path.home()))
    except ValueError:
        return str(path)


class ElidedLabel(QLabel):
    """Keep full accessible text and a tooltip, without widening the window."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(22)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self.palette().color(QPalette.ColorRole.WindowText))
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideMiddle, self.width())
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

    def minimumSizeHint(self):
        return QSize(0, 22)


class ArrowComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet('QComboBox::down-arrow { image: none; width: 0; height: 0; }')

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(self.palette().color(QPalette.ColorRole.Text), 1.5,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        x, y = self.width() - 17, self.height() / 2
        p.drawPolyline(QPolygonF([QPointF(x - 4, y - 2), QPointF(x, y + 2), QPointF(x + 4, y - 2)]))


class CheckBox(QCheckBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        if self.isChecked():
            option = QStyleOptionButton()
            self.initStyleOption(option)
            rect = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, self)
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(QPen(QColor('white'), 2, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            x, y = rect.center().x(), rect.center().y()
            p.drawPolyline(QPolygonF([QPointF(x - 4, y), QPointF(x - 1, y + 3), QPointF(x + 5, y - 4)]))


class CourseGlyph(QWidget):
    def __init__(self, parent=None, *, files=False, small=False):
        super().__init__(parent)
        self.files = files
        self.setFixedSize(44 if small else 104, 44 if small else 104)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(self.width() / 104, self.height() / 104)
        accent = QColor(self.window().property('themeAccent') or '#4c91ff')
        wash = QColor(accent)
        wash.setAlpha(24)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(wash)
        p.drawRoundedRect(QRectF(2, 2, 100, 100), 26, 26)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(accent, 2.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        if self.files:
            p.drawRoundedRect(QRectF(29, 22, 46, 60), 7, 7)
            for y, width in ((39, 24), (51, 24), (63, 15)):
                p.drawLine(QPointF(40, y), QPointF(40 + width, y))
        else:
            p.drawRoundedRect(QRectF(21, 28, 62, 44), 8, 8)
            p.drawLine(QPointF(38, 83), QPointF(66, 83))
            p.setBrush(accent)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(QPolygonF([QPointF(46, 40), QPointF(46, 61), QPointF(63, 50.5)]))


class EmptyState(QWidget):
    def __init__(self, title, description, parent=None, *, files=False):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 8 if files else 16, 24, 8 if files else 16)
        layout.setSpacing(8)
        layout.addStretch(2)
        glyph = CourseGlyph(files=files)
        glyph.setFixedSize(64 if files else 88, 64 if files else 88)
        layout.addWidget(glyph, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(8)
        self.title = QLabel(title)
        self.title.setProperty('role', 'emptyTitle')
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title)
        self.description = QLabel(description)
        self.description.setProperty('role', 'muted')
        self.description.setWordWrap(True)
        self.description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.description.setFixedWidth(440)
        self.description.setMinimumHeight(48)
        layout.addWidget(self.description, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(3)


def apply_theme(window, dark=None):
    if dark is None:
        dark = QApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128
    if dark:
        bg, surface, field, border = '#1b1d22', '#24272e', '#1c1f25', '#373c46'
        fg, muted, accent, hover, selected = '#f0f2f6', '#a4acb9', '#4c91ff', '#303540', '#283c59'
        disabled, banner = '#656d7b', '#3b3424'
    else:
        bg, surface, field, border = '#f4f6f9', '#ffffff', '#ffffff', '#dce1e9'
        fg, muted, accent, hover, selected = '#202735', '#697487', '#246de3', '#edf1f7', '#e6efff'
        disabled, banner = '#929caa', '#fff2d5'
    palette = QPalette(QApplication.palette())
    for role, color in ((QPalette.ColorRole.Window, bg), (QPalette.ColorRole.Base, surface),
                        (QPalette.ColorRole.AlternateBase, bg), (QPalette.ColorRole.WindowText, fg),
                        (QPalette.ColorRole.Text, fg), (QPalette.ColorRole.Button, surface),
                        (QPalette.ColorRole.ButtonText, fg), (QPalette.ColorRole.Highlight, accent),
                        (QPalette.ColorRole.HighlightedText, '#ffffff'), (QPalette.ColorRole.PlaceholderText, muted)):
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(disabled))
    window.setPalette(palette)
    window.setProperty('themeAccent', accent)
    window.setStyleSheet(f'''
        QWidget {{ font-size: 13px; color: {fg}; }}
        QWidget#appRoot {{ background: {bg}; }}
        QLabel {{ background: transparent; border: none; }}
        QLabel[role="brand"] {{ font-size: 25px; font-weight: 600; }}
        QLabel[role="muted"] {{ color: {muted}; }}
        QLabel[role="sectionTitle"] {{ font-size: 17px; font-weight: 600; }}
        QLabel[role="emptyTitle"] {{ font-size: 22px; font-weight: 600; }}
        QLabel[role="badge"] {{ color: {muted}; background: {surface}; border: 1px solid {border};
            border-radius: 12px; padding: 6px 12px; }}
        QLabel[role="notice"] {{ background: {banner}; border-radius: 8px; padding: 10px 14px; }}
        QFrame[role="card"] {{ background: {surface}; border: 1px solid {border}; border-radius: 12px; }}
        QTabWidget::pane {{ border: none; background: transparent; padding-top: 12px; }}
        QTabBar::tab {{ background: transparent; color: {muted}; border: none;
            padding: 10px 20px; margin-right: 6px; border-radius: 8px; }}
        QTabBar::tab:selected {{ background: {selected}; color: {accent}; font-weight: 600; }}
        QTabBar::tab:hover:!selected {{ background: {hover}; }}
        QTabWidget#settingsSections::pane {{ background: {surface}; border: 1px solid {border};
            border-radius: 10px; padding: 4px; }}
        QTabWidget#settingsSections QTabBar::tab {{ padding: 8px 14px; margin-bottom: 8px; }}
        QScrollArea {{ border: none; background: transparent; }}
        QScrollArea > QWidget > QWidget {{ background: transparent; }}
        QPushButton, QToolButton {{ background: {surface}; border: 1px solid {border};
            border-radius: 7px; padding: 7px 14px; min-height: 18px; }}
        QPushButton:hover, QToolButton:hover {{ background: {hover}; }}
        QPushButton:pressed, QToolButton:pressed, QToolButton:checked {{ background: {selected}; }}
        QPushButton:disabled, QToolButton:disabled {{ color: {disabled}; background: transparent; }}
        QPushButton[role="primary"] {{ background: {accent}; color: white; border-color: {accent}; font-weight: 600; }}
        QPushButton[role="primary"]:hover {{ background: #397ef0; }}
        QPushButton[role="primary"]:disabled {{ background: {hover}; border-color: {border}; color: {disabled}; }}
        QPushButton[role="quiet"], QToolButton[role="quiet"] {{ background: transparent; border-color: transparent; color: {muted}; }}
        QPushButton[role="quiet"]:hover, QToolButton[role="quiet"]:hover {{ background: {hover}; color: {fg}; }}
        QPushButton:focus, QToolButton:focus {{ border-color: {accent}; }}
        QToolButton::menu-indicator {{ subcontrol-position: right center; right: 6px; }}
        QLineEdit, QSpinBox, QComboBox {{ background: {field}; border: 1px solid {border};
            border-radius: 6px; padding: 7px 10px; min-height: 18px; selection-background-color: {accent}; }}
        QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {accent}; }}
        QComboBox {{ padding-right: 24px; }}
        QComboBox::drop-down {{ width: 24px; border: none; }}
        QComboBox QAbstractItemView {{ background: {surface}; selection-background-color: {selected}; selection-color: {fg}; }}
        QTreeWidget, QTableWidget, QPlainTextEdit {{ background: {surface}; alternate-background-color: {bg};
            border: 1px solid {border}; border-radius: 8px; selection-background-color: {selected};
            selection-color: {fg}; outline: none; }}
        QTreeView::item, QTableView::item {{ padding: 7px 6px; border: none; }}
        QHeaderView {{ background: {surface}; }}
        QHeaderView::section {{ background: {surface}; color: {muted}; border: none;
            border-bottom: 1px solid {border}; padding: 9px 8px; }}
        QTableCornerButton::section {{ background: {surface}; border: none; }}
        QProgressBar {{ background: {hover}; border: none; border-radius: 2px; }}
        QProgressBar::chunk {{ background: {accent}; border-radius: 2px; }}
        QCheckBox {{ spacing: 8px; }}
        QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {border}; border-radius: 4px; background: {field}; }}
        QCheckBox::indicator:checked {{ background: {accent}; border-color: {accent}; }}
        QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: {border}; border-radius: 3px; min-height: 30px; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 2px; }}
        QScrollBar::handle:horizontal {{ background: {border}; border-radius: 3px; min-width: 30px; }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}
        QMenu {{ background: {surface}; border: 1px solid {border}; padding: 6px; }}
        QMenu::item {{ padding: 7px 24px; border-radius: 5px; }}
        QMenu::item:selected {{ background: {selected}; }}
        QMenu::item:disabled {{ color: {disabled}; }}
        QToolTip {{ color: {fg}; background: {surface}; border: 1px solid {border}; padding: 6px; }}
    ''')
