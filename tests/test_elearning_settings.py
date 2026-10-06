import json

import pytest

pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication, QDialog, QTableWidgetItem

from src.elearning_panel import ElearningPanel
from src.elearning_settings import ElearningSettingsDialog, editable_config, save_config


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    return QApplication.instance() or QApplication([])


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'import' / 'config.json'
    path.parent.mkdir()
    path.write_text(json.dumps(dict(base_url='https://elearning.fudan.edu.cn', root=str(tmp_path/'courses'),
        state_dir='history', max_bytes=50000000,
        courses=[dict(id='111', name='Test', directory='Test/课件')])))
    return path


def test_edit_save_reopen_and_launch_use_persistent_config(app, source, tmp_path, monkeypatch):
    destination = tmp_path / 'personal' / 'elearning.json'
    original = source.read_bytes()
    dialog = ElearningSettingsDialog(source, destination)
    dialog.root.setText(str(tmp_path / 'new-root'))
    dialog.max_bytes.setValue(20000000)
    dialog.add_course(dict(id='222', name='Second', directory='Second/课件'))
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert source.read_bytes() == original
    data = editable_config(destination)
    assert data['root'] == str(tmp_path / 'new-root') and data['max_bytes'] == 20000000
    assert data['state_dir'] == str(source.parent / 'history')
    assert len(data['courses']) == 2
    assert not (tmp_path / 'new-root').exists()  # Editing is not syncing or moving files.
    panel = ElearningPanel(config_store=destination)
    assert panel.config_path.text() == str(destination)
    assert panel.command()[panel.command().index('--config')+1] == str(destination)
    assert '20 MB' in panel.policy.text() and '2 门课程' in panel.save_location.text()
    assert '20,000,000' in panel.policy.toolTip()
    assert not panel.is_running()
    panel.set_running(True)
    assert not panel.edit_config.isEnabled()
    panel.set_running(False)
    assert panel.edit_config.isEnabled()
    panel.close()
    dialog.close()


@pytest.mark.parametrize('invalid', ['escape', 'duplicate', 'overlap', 'blank', 'relative-root'])
def test_invalid_edits_leave_saved_config_untouched(app, source, tmp_path, invalid):
    destination = tmp_path/'personal.json'
    save_config(destination, editable_config(source))
    before = destination.read_bytes()
    dialog = ElearningSettingsDialog(destination, destination)
    if invalid == 'escape':
        dialog.courses.setItem(0, 2, QTableWidgetItem('../escape'))
    elif invalid == 'duplicate':
        dialog.add_course(dict(id='111', name='Duplicate', directory='Another/课件'))
    elif invalid == 'overlap':
        dialog.add_course(dict(id='222', name='Overlap', directory='Test/课件/sub'))
    elif invalid == 'blank':
        dialog.add_course()
    else:
        dialog.root.setText('relative-root')
    dialog.save()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert '未保存' in dialog.error.text()
    assert destination.read_bytes() == before
    dialog.close()


def test_cancel_does_not_save(app, source, tmp_path):
    destination = tmp_path/'not-created.json'
    dialog = ElearningSettingsDialog(source, destination)
    dialog.root.setText(str(tmp_path/'other'))
    dialog.reject()
    assert not destination.exists()


def test_apply_settings_updates_current_panel_without_starting_task(app, source, tmp_path, monkeypatch):
    destination = tmp_path/'personal.json'
    panel = ElearningPanel(config_store=destination)
    def accept(dialog):
        dialog.root.setText(str(tmp_path/'chosen'))
        dialog.save()
        return dialog.result()
    monkeypatch.setattr(ElearningSettingsDialog, 'exec', accept)
    panel.configure(str(source))
    assert panel.config_path.text() == str(destination)
    assert str(tmp_path/'chosen') in panel.save_location.text()
    assert not panel.is_running()
    panel.close()


@pytest.mark.parametrize('bad', [None, [], {}, {'base_url': 123, 'root': '/', 'courses': []}])
def test_malformed_import_is_reported_without_replacing_personal_settings(app, source, tmp_path, bad):
    destination = tmp_path/'personal.json'
    save_config(destination, editable_config(source))
    before = destination.read_bytes()
    invalid = tmp_path/'invalid.json'
    invalid.write_text(json.dumps(bad))
    panel = ElearningPanel(config_store=destination)
    panel.configure(str(invalid))
    assert '无法读取课程配置' in panel.status.text()
    assert destination.read_bytes() == before
    panel.close()
