"""Exercise permission failures without saved credentials, dialogs or course work."""

from uuid import uuid4

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from src import mac_gui
from src.preferences import Preferences, defaults
from src.storage_access import StorageAccessError


@pytest.fixture
def qt_app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


def test_denied_folder_stops_gui_before_engine_launch(qt_app, monkeypatch, tmp_path):
    values = defaults()
    values.update(mode="summarize", course_ids="12345", llm_base_url_1="https://example.invalid",
                  llm_models_1="test-model", llm_api_key_1=uuid4().hex,
                  out_dir=str(tmp_path / "videos"), summary_dir=str(tmp_path / "notes"))
    window = mac_gui.MainWindow(initial_values=values)
    messages = []

    def denied(video, notes, **kwargs):
        assert str(notes) == values["summary_dir"]
        raise StorageAccessError("test permission denial")

    monkeypatch.setattr(mac_gui, "check_pipeline_storage", denied)
    monkeypatch.setattr(window.process, "start", lambda *a: pytest.fail("Unexpected engine launch"))
    monkeypatch.setattr(mac_gui.QMessageBox, "warning", lambda *a: messages.append(a[-1]))
    window.launch("task")
    assert messages == ["test permission denial"]
    assert window.start.isEnabled()
    window.close()


def test_saved_paths_reappear_when_window_is_recreated(qt_app, tmp_path):
    class MemoryKeyring:
        def __init__(self):
            self.values = {}

        def get_password(self, service, key):
            return self.values.get((service, key))

        def set_password(self, service, key, value):
            self.values[service, key] = value

    path = tmp_path / "settings.json"
    vault = MemoryKeyring()
    first = mac_gui.MainWindow(preferences=Preferences(path, vault), initial_values=defaults())
    first.fields["out_dir"].setText(str(tmp_path / "external videos"))
    first.fields["summary_dir"].setText(str(tmp_path / "Documents" / "notes"))
    assert first.save()
    first.close()
    second = mac_gui.MainWindow(preferences=Preferences(path, vault))
    assert second.fields["out_dir"].text() == str(tmp_path / "external videos")
    assert second.fields["summary_dir"].text() == str(tmp_path / "Documents" / "notes")
    second.close()


def test_scrolling_settings_does_not_change_budget_or_model(qt_app):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    window = mac_gui.MainWindow(initial_values=defaults())
    for name in ('llm_output_tokens', 'asr_timeout_seconds', 'asr_response_format', 'mode',
                 'asr_provider', 'asr_dashscope_model'):
        widget = window.fields[name]
        before = window.collect()[name]
        widget.setFocus()
        event = QWheelEvent(QPointF(4, 4), QPointF(4, 4), QPoint(), QPoint(0, -120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(widget, event)
        assert window.collect()[name] == before
    window.fields['llm_output_tokens'].setValue(1024)
    window.reset_note_budget()
    assert window.collect()['llm_output_tokens'] == 65536
    window.close()


def test_provider_switch_preserves_both_configurations_and_keys(qt_app):
    from src.preferences import runtime_environment
    values = defaults()
    values.update(asr_api_key=uuid4().hex, asr_dashscope_api_key=uuid4().hex)
    window = mac_gui.MainWindow(initial_values=values)
    window.fields['asr_provider'].setCurrentIndex(1)
    window.fields['asr_dashscope_model'].setCurrentIndex(1)
    assert window.fields['asr_model'].isHidden()
    assert not window.fields['asr_dashscope_model'].isHidden()
    selected = window.collect()
    env = runtime_environment(selected, {})
    assert env['ASR_MODEL'] == 'paraformer-v2'
    assert env['ASR_API_KEY'] == values['asr_dashscope_api_key']
    window.fields['asr_provider'].setCurrentIndex(0)
    selected = window.collect()
    assert selected['asr_dashscope_model'] == 'paraformer-v2'
    assert selected['asr_model'] == values['asr_model']
    assert runtime_environment(selected, {})['ASR_API_KEY'] == values['asr_api_key']
    window.close()
