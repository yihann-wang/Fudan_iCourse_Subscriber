"""Editable, non-secret eLearning settings kept outside the installed package."""

from pathlib import Path

from platformdirs import user_config_path
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .artifacts import atomic_write_json
from .elearning_helper.config import HARD_LIMIT, load_config, parse_config


def personal_config_path():
    return user_config_path('Fudan iCourse Subscriber', appauthor=False) / 'elearning.json'


def editable_config(filename):
    """Normalize relative state paths while preserving their existing location."""
    config = load_config(filename)
    return dict(base_url=config.base_url, root=str(config.root), state_dir=str(config.state_dir),
                max_bytes=config.max_bytes, auth_env=config.auth_env, timezone=config.timezone,
                download_hosts=list(config.download_hosts), courses=[
                    dict(id=c.id, name=c.name, directory=str(c.directory.relative_to(config.root)))
                    for c in config.courses])


def save_config(filename, data):
    # Validate before any mutation; an invalid edit leaves the last good config intact.
    parse_config(data, filename)
    atomic_write_json(filename, data, private=True)


class ElearningSettingsDialog(QDialog):
    def __init__(self, filename, destination, parent=None):
        super().__init__(parent)
        self.destination = Path(destination)
        self.original = editable_config(filename)
        self.setWindowTitle('eLearning 保存位置与课程设置')
        self.resize(880, 570)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.root = self.add_directory(form, '文件保存位置', self.original['root'])
        self.state_dir = self.add_directory(form, '同步记录位置', self.original['state_dir'])
        self.max_bytes = QSpinBox()
        self.max_bytes.setRange(1, HARD_LIMIT)
        self.max_bytes.setValue(self.original['max_bytes'])
        self.max_bytes.setGroupSeparatorShown(True)
        self.max_bytes.setSuffix(' 字节')
        form.addRow('单文件大小须小于', self.max_bytes)
        layout.addLayout(form)
        hint = QLabel('保存位置下按表格中的子目录存放文件。更改位置不会搬移已有文件或同步记录；'
                      '使用新的同步记录目录会重新建立作业基线。最大文件上限为 50,000,000 字节。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addWidget(QLabel('同步课程（eLearning 课程 ID 与录像课程 ID 不同）'))
        self.courses = QTableWidget(0, 3)
        self.courses.setHorizontalHeaderLabels(['eLearning 课程 ID', '课程名称', '保存子目录（相对于上面的保存位置）'])
        self.courses.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.courses.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.courses.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.courses.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for course in self.original['courses']:
            self.add_course(course)
        layout.addWidget(self.courses, 1)
        row = QHBoxLayout()
        add = QPushButton('添加课程')
        add.clicked.connect(lambda: self.add_course())
        remove = QPushButton('移除所选课程')
        remove.clicked.connect(self.remove_courses)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch()
        layout.addLayout(row)
        self.error = QLabel('')
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存配置')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def add_directory(self, form, title, value):
        row = QHBoxLayout()
        field = QLineEdit(value)
        button = QPushButton('选择…')
        def choose():
            selected = QFileDialog.getExistingDirectory(self, title, field.text())
            if selected:
                field.setText(selected)
        button.clicked.connect(choose)
        row.addWidget(field, 1)
        row.addWidget(button)
        form.addRow(title, row)
        return field

    def add_course(self, course=None):
        course = course or dict(id='', name='', directory='')
        row = self.courses.rowCount()
        self.courses.insertRow(row)
        for column, key in enumerate(('id', 'name', 'directory')):
            self.courses.setItem(row, column, QTableWidgetItem(course[key]))

    def remove_courses(self):
        for row in sorted({i.row() for i in self.courses.selectedIndexes()}, reverse=True):
            self.courses.removeRow(row)

    def collect(self):
        data = dict(self.original)
        courses = []
        for row in range(self.courses.rowCount()):
            course = {key: self.courses.item(row, column).text().strip()
                      for column, key in enumerate(('id', 'name', 'directory'))}
            if not all(course.values()):
                raise ValueError(f'第 {row + 1} 行的课程 ID、名称和保存子目录都需要填写。')
            courses.append(course)
        root, state = (Path(w.text().strip()).expanduser() for w in (self.root, self.state_dir))
        if not root.is_absolute() or not state.is_absolute():
            raise ValueError('文件保存位置和同步记录位置都需要使用绝对路径。')
        data.update(root=str(root), state_dir=str(state), max_bytes=self.max_bytes.value(), courses=courses)
        return data

    def save(self):
        try:
            save_config(self.destination, self.collect())
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.error.setText(f'配置未保存：{exc}')
            return
        self.accept()
