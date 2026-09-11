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
