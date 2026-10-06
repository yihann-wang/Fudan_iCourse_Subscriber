"""Use two real offline workers to verify independent workspace lifecycles."""
import json
import sys
import time
from copy import deepcopy

import pytest

pytest.importorskip('PySide6')
from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication

from src.mac_gui import MainWindow
from src.preferences import Preferences, defaults
from src.workspace_paths import check_parallel_paths


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path):
    e = dict(base_url='https://elearning.fudan.edu.cn', root=str(tmp_path/'courses'),
             state_dir=str(tmp_path/'state'), max_bytes=50000000,
             courses=[dict(id='99', name='Fixture', directory='101-Fixture/课件')])
    values = dict(defaults(), mode='download', course_ids='101', stu_id='FIXTURE',
                  uis_psw='OLD_TEST_PASSWORD', out_dir=str(tmp_path/'courses'),
                  summary_dir=str(tmp_path/'notes'), elearning=e)
    w = MainWindow(preferences=Preferences(tmp_path/'settings.json'), initial_values=values)
    yield w
    if w.video_is_running() or w.elearning.is_running():
        w.close()
        pump(app, lambda: not w.video_is_running() and not w.elearning.is_running())
    w.close()


def pump(app, condition, timeout=8):
    deadline = time.monotonic()+timeout
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    app.processEvents()
    assert condition(), 'Offline worker did not reach the expected state'


def workers(window, monkeypatch):
    start = window.process.start
    video = '\n'.join([
        'import os,signal,time,sys', 'os.setsid()',
        'signal.signal(signal.SIGTERM, lambda *_: sys.exit(130))',
        "print('VIDEO_READY', flush=True)", 'while True: time.sleep(.01)',
    ])
    monkeypatch.setattr(window.process, 'start', lambda *_: start(sys.executable, ['-c', video]))
    monkeypatch.setattr('src.mac_gui.check_pipeline_storage', lambda *a, **k: None)
    panel = window.elearning
    original = panel.command
    script = '\n'.join([
        'import json,signal,time,sys',
        "assert json.load(sys.stdin)['password']=='OLD_TEST_PASSWORD'",
        'with open(sys.argv[1]) as f: config=json.load(f)',
        'def stop(*_):', "    print(json.dumps(dict(results=[])),flush=True)", '    sys.exit(130)',
        'signal.signal(signal.SIGINT, stop)',
        "print('ELEARNING_READY',file=sys.stderr,flush=True)", 'while True: time.sleep(.01)',
    ])
    def command():
        args=original()
        return ['-c', script, args[args.index('--config')+1]]
    monkeypatch.setattr(panel, 'command', command)


def ready(app, window, kind):
    if kind=='video':
        window.launch('task')
        pump(app, lambda: 'VIDEO_READY' in window.panel.diagnostics)
    else:
        window.elearning.launch('sync')
        pump(app, lambda: 'ELEARNING_READY' in window.elearning.output.toPlainText())


def test_configuration_is_only_editable_in_settings(window):
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == ['录像与笔记','eLearning 与作业','设置']
    assert all(window.settings.isAncestorOf(w) for w in window.fields.values())
    assert window.settings.isAncestorOf(window.settings.elearning_editor.courses)
    assert window.elearning.config_path.isHidden() and window.elearning.edit_config.isHidden()
    assert window.panel.log.isHidden()
    assert not hasattr(window, 'toggle_settings')


def test_one_atomic_save_reopens_both_modules_and_rejects_invalid_drafts(window, tmp_path):
    window.fields['out_dir'].setText(str(tmp_path/'new-videos'))
    window.settings.elearning_editor.root.setText(str(tmp_path/'new-files'))
    window.settings.save_button.click()
    assert not window.dirty
    saved=window.preferences.path.read_bytes()
    data=json.loads(saved)
    assert data['uis_psw']=='OLD_TEST_PASSWORD'
    assert data['elearning']['root']==str(tmp_path/'new-files')
    assert not (tmp_path/'elearning.json').exists()
    restored=MainWindow(preferences=Preferences(window.preferences.path))
    assert restored.values==window.values
    restored.close()
    window.settings.elearning_editor.add_course()
    assert window.dirty and not window.start.isEnabled() and not window.elearning.sync_button.isEnabled()
    assert not window.save() and window.preferences.path.read_bytes()==saved


def test_failed_save_keeps_committed_values_and_work_summary(window, monkeypatch, tmp_path):
    old=deepcopy(window.values)
    summary=window.video_summary.text()
    window.fields['out_dir'].setText(str(tmp_path/'draft'))
    monkeypatch.setattr(window.preferences,'save',lambda *_: (_ for _ in ()).throw(OSError('full')))
    assert not window.save()
    assert window.values==old and window.video_summary.text()==summary
    assert window.dirty and not window.start.isEnabled()


@pytest.mark.parametrize('first',['video','elearning'])
@pytest.mark.parametrize('stop_first',['video','elearning'])
def test_parallel_start_save_snapshot_and_independent_stop(app,window,monkeypatch,tmp_path,first,stop_first):
    workers(window,monkeypatch)
    ready(app,window,first)
    ready(app,window,'elearning' if first=='video' else 'video')
    assert all(window.tabs.isTabEnabled(i) for i in range(3))
    video_pid,e_pid=window.process.processId(),window.elearning.process.processId()
    window.launch('task');window.elearning.launch('sync')
    assert (video_pid,e_pid)==(window.process.processId(),window.elearning.process.processId())
    old=deepcopy(window.values)
    snapshot=window.elearning._snapshot_path
    snapshot_bytes=snapshot.read_bytes()
    window.fields['out_dir'].setText(str(tmp_path/'next-videos'))
    window.fields['uis_psw'].setText('NEXT_TEST_PASSWORD')
    window.settings.elearning_editor.root.setText(str(tmp_path/'next-files'))
    assert window.save()
    assert window.run_values==old and snapshot.read_bytes()==snapshot_bytes
    assert window.process.processEnvironment().value('UISPsw')=='OLD_TEST_PASSWORD'
    assert old['out_dir'] == window.video_location.toolTip()
    assert old['elearning']['root'] in window.elearning.save_location.text()
    assert window.stop.isEnabled() and window.elearning.stop_button.isEnabled()
    if stop_first=='video':
        window.cancel()
        pump(app,lambda:not window.video_is_running())
        assert window.elearning.is_running() and window.elearning.process.processId()==e_pid
        assert not window.elearning.sync_button.isEnabled()
        window.elearning.stop()
    else:
        window.elearning.stop()
        pump(app,lambda:not window.elearning.is_running())
        assert window.video_is_running() and window.process.processId()==video_pid
        assert not window.start.isEnabled()
        window.cancel()
    pump(app,lambda:not window.video_is_running() and not window.elearning.is_running())
    assert not snapshot.exists()
    assert window.start.isEnabled() and window.elearning.sync_button.isEnabled()
    assert str(tmp_path/'next-videos') == window.video_location.toolTip()
    assert str(tmp_path/'next-files') in window.elearning.save_location.text()


def test_close_stops_both_workers_and_removes_config_snapshot(app,window,monkeypatch):
    workers(window,monkeypatch)
    window.show()
    ready(app,window,'video');ready(app,window,'elearning')
    snapshot=window.elearning._snapshot_path
    window.close()
    assert window.closing
    pump(app,lambda:not window.video_is_running() and not window.elearning.is_running())
    assert not window.isVisible() and not snapshot.exists()


def test_failed_start_cleans_snapshot_without_stopping_video(app,window,monkeypatch):
    workers(window,monkeypatch)
    ready(app,window,'video')
    real_start=window.elearning.process.start
    monkeypatch.setattr(window.elearning.process,'start',lambda *_:real_start('/nonexistent/icourse-fixture',[]))
    window.elearning.launch('sync')
    pump(app,lambda:not window.elearning.is_running())
    assert window.video_is_running() and window.elearning._snapshot_dir is None
    assert window.elearning.sync_button.isEnabled() and not window.start.isEnabled()


def test_shared_course_root_is_allowed_but_overlapping_write_scope_is_not(window):
    video=window.values
    e=deepcopy(video['elearning'])
    check_parallel_paths(video,e)
    for target in ['101-Fixture','101-Fixture/录屏','101-Fixture/录屏/nested']:
        e['courses'][0]['directory']=target
        with pytest.raises(ValueError,match='重叠'):
            check_parallel_paths(video,e)


def test_import_is_a_draft_until_saved(window, tmp_path):
    old = deepcopy(window.values)
    data = deepcopy(old['elearning'])
    data['root'] = str(tmp_path / 'imported')
    source = tmp_path / 'import.json'
    source.write_text(json.dumps(data))
    window.settings.import_config(str(source))
    assert window.dirty and window.values == old
    assert window.save() and window.values['elearning']['root'] == data['root']
    assert json.loads(source.read_text()) == data


def test_old_elearning_file_is_imported_and_unified_save_takes_precedence(app, tmp_path):
    data = dict(base_url='https://elearning.fudan.edu.cn', root=str(tmp_path/'files'),
                state_dir=str(tmp_path/'state'), max_bytes=50000000,
                courses=[dict(id='99', name='Fixture', directory='course/课件')])
    legacy = tmp_path / 'elearning.json'
    legacy.write_text(json.dumps(data))
    prefs = Preferences(tmp_path / 'settings.json')
    w = MainWindow(preferences=prefs, elearning_config_store=legacy)
    assert w.values['elearning']['root'] == data['root']
    assert w.save()
    w.close()
    legacy.write_text('invalid legacy content')
    restored = MainWindow(preferences=prefs, elearning_config_store=legacy)
    assert restored.values['elearning']['root'] == data['root']
    restored.close()


@pytest.mark.parametrize('kind', ['video', 'elearning'])
def test_close_immediately_after_start_does_not_leave_worker(app, window, monkeypatch, kind):
    workers(window, monkeypatch)
    window.show()
    if kind == 'video':
        window.launch('task')
    else:
        window.elearning.launch('sync')
    window.close()
    pump(app, lambda: not window.video_is_running() and not window.elearning.is_running())
    assert not window.isVisible() and window.elearning._snapshot_dir is None


def test_video_failed_start_does_not_stop_elearning(app, window, monkeypatch):
    workers(window, monkeypatch)
    ready(app, window, 'elearning')
    real_start = QProcess.start
    monkeypatch.setattr(window.process, 'start', lambda *_: real_start(window.process, '/nonexistent/icourse-fixture', []))
    window.launch('task')
    pump(app, lambda: not window.video_is_running())
    assert window.elearning.is_running() and window.start.isEnabled()
    assert not window.elearning.sync_button.isEnabled()


def test_elearning_completion_keeps_video_running(app, window, monkeypatch):
    workers(window, monkeypatch)
    ready(app, window, 'video')
    monkeypatch.setattr(window.elearning, 'command', lambda: ['-c',
        'import sys,json; json.load(sys.stdin); print(json.dumps(dict(results=[])))'])
    window.elearning.launch('sync')
    pump(app, lambda: not window.elearning.is_running())
    assert '完成' in window.elearning.status.text() and window.video_is_running()
    assert window.elearning._snapshot_dir is None and window.elearning.sync_button.isEnabled()
