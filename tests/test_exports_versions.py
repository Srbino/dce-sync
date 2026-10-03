import hashlib
import json
import threading
import tarfile
from datetime import date
from types import SimpleNamespace

import pytest

import dce_exports
import dce_versions
import dce_sync
from dce_dashboard import Dashboard


def app_at(tmp_path):
    config = tmp_path / 'channels.yaml'
    config.write_text('output_dir: exports\nchannels: {chat: {id: "123"}}')
    return Dashboard(config)


def test_standalone_export_does_not_change_archive_or_checkpoint(tmp_path, monkeypatch):
    app = app_at(tmp_path)
    app.output.mkdir()
    old = app.output / 'chat [123] (pulled 2026-01-01).json'
    old.write_text('{"channel":{"id":"123"},"messages":[]}')
    before = old.read_bytes()
    app.operation = 'export'
    app.export_options = dce_exports.validate(dict(format='HtmlDark', filter='has:image', after='2026-01-01', partition='20mb'))
    binary = tmp_path / 'exporter'
    binary.write_text('''#!/usr/bin/env python3
import sys
from pathlib import Path
assert sys.argv[sys.argv.index('-f')+1]=='HtmlDark'
assert sys.argv[sys.argv.index('--filter')+1]=='has:image'
assert sys.argv[sys.argv.index('--partition')+1]=='20mb'
out=Path(sys.argv[sys.argv.index('-o')+1])
(out/'chat.html').write_text('<html>Export</html>')
''')
    binary.chmod(0o755)
    monkeypatch.setattr(dce_sync, 'load_token', lambda _: 'placeholder')
    monkeypatch.setattr(dce_sync, 'find_dce_binary', lambda: str(binary))
    app.run('export', {'chat'})
    assert app.rows[0]['status'] == 'done'
    assert list((app.output / 'reports').rglob('*.html'))
    assert old.read_bytes() == before
    assert dce_sync.parse_last_after(app.output, '123') == date(2026, 1, 1)
    assert dce_sync.export_files(app.output) == [old]


def test_snapshot_preserves_media_hashes_and_excludes_partial_data(tmp_path):
    app = app_at(tmp_path)
    archive = app.output / 'archive/Guild/chat [123]/messages [123].json'
    archive.parent.mkdir(parents=True)
    archive.write_text('{"messages":[]}')
    media = app.output / 'media/123/image.png'
    media.parent.mkdir(parents=True)
    media.write_bytes(b'picture')
    for folder in ['.downloads', 'reports']:
        path = app.output / folder / 'incomplete.json'
        path.parent.mkdir(parents=True)
        path.write_text('excluded')
    target = dce_versions.snapshot(app.output, app.config, tmp_path / 'versions', threading.Event(), lambda *a: None)
    with tarfile.open(target) as tf:
        assert 'exports/media/123/image.png' in tf.getnames()
        assert not any('incomplete' in name for name in tf.getnames())
        manifest = json.load(tf.extractfile('manifest.json'))
        for entry in manifest['files']:
            assert hashlib.sha256(tf.extractfile(entry['path']).read()).hexdigest() == entry['sha256']
    assert target.stat().st_mode & 0o777 == 0o600
    stop = threading.Event()
    stop.set()
    with pytest.raises(InterruptedError):
        dce_versions.snapshot(app.output, app.config, tmp_path / 'versions', stop, lambda *a: None)
    assert list((tmp_path / 'versions').iterdir()) == [target]


def test_folder_open_reports_failure_and_handles_paths(tmp_path, monkeypatch):
    import dce_dashboard
    monkeypatch.setattr(Dashboard, 'check_engine', lambda self: None)
    app = app_at(tmp_path)
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(dce_dashboard.subprocess, 'run', run)
    monkeypatch.setattr(dce_dashboard.shutil, 'which', lambda _: '/usr/bin/xdg-open')
    assert app.open_folder()['path'] == str(app.output)
    assert commands[-1][-1] == str(app.output)
    with pytest.raises(ValueError): app.open_folder('../../etc')
    monkeypatch.setattr(dce_dashboard.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1))
    with pytest.raises(ValueError, match='file manager'): app.open_folder()


def test_direct_messages_use_explicit_dm_command(monkeypatch):
    import dce_discovery
    monkeypatch.setattr(dce_sync, 'load_token', lambda _: 'placeholder')
    monkeypatch.setattr(dce_sync, 'find_dce_binary', lambda: 'exporter')
    def run(command, **kwargs):
        assert command == ['exporter', 'dm']
        return SimpleNamespace(returncode=0, stdout='123 | Alice / Bob\n', stderr='')
    monkeypatch.setattr(dce_discovery.subprocess, 'run', run)
    assert dce_discovery.discover('@me') == [dict(id='123', name='Alice / Bob', category='Direct messages', kind='dm')]


def test_export_options_validation():
    for values in [dict(format='exec'), dict(reverse='true'), dict(partition='0'), dict(locale='../../file'), dict(filter='x\ny')]:
        with pytest.raises(ValueError): dce_exports.validate(values)
    assert dce_exports.validate(dict(partition='20mb'))['partition'] == '20mb'
