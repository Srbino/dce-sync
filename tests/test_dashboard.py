import json
import threading
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

import pytest

import dce_sync as dce
from dce_archive import archive_lock, consolidate
from dce_dashboard import Dashboard, make_handler


def export(path, messages, cid='123', **extra):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(channel={'id': cid}, messages=messages, **extra)))
    return path


def msg(mid, content='hello'):
    return dict(id=mid, timestamp='2026-08-01T12:00:00+00:00', content=content)


def test_merge_repeat_and_newest_edit(tmp_path):
    export(tmp_path / 'Guild - chat [123] (pulled 2026-08-01).json', [msg('1'), msg('2')])
    stage = tmp_path / '.downloads' / 'one'
    incoming = export(stage / 'chat [123].json', [msg('2', 'edited'), msg('3')])
    result = consolidate(tmp_path, 'chat', {'id':'123'}, [incoming], date(2026, 8, 2))
    assert result['messages'] == 3
    assert result['duplicates'] == 1
    target = tmp_path / result['path']
    assert json.loads(target.read_text())['messages'][1]['content'] == 'edited'
    assert dce.parse_last_after(tmp_path, '123') == date(2026, 8, 2)
    assert len(dce._files_for_channel(tmp_path, '123')) == 1
    assert not incoming.exists()
    again = consolidate(tmp_path, 'chat', {'id':'123'})
    assert again['path'] == result['path']
    assert again['messages'] == 3


def test_corrupt_or_wrong_channel_preserves_sources(tmp_path):
    old = export(tmp_path / 'old [123].json', [msg('1')])
    incoming = export(tmp_path / '.downloads/new.json', [msg('2')], cid='456')
    with pytest.raises(ValueError):
        consolidate(tmp_path, 'chat', {'id':'123'}, [incoming])
    assert old.exists() and incoming.exists()
    incoming.write_text('{')
    with pytest.raises(ValueError):
        consolidate(tmp_path, 'chat', {'id':'123'}, [incoming])
    assert old.exists()


def test_interrupted_commit_preserves_original(tmp_path, monkeypatch):
    old = export(tmp_path / 'old [123].json', [msg('1')])
    import dce_archive
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(dce_archive.os, 'replace', fail)
    with pytest.raises(OSError):
        consolidate(tmp_path, 'chat', {'id':'123'})
    assert json.loads(old.read_text())['messages'][0]['id'] == '1'
    assert not list(tmp_path.rglob('*.tmp'))


def test_staging_excluded_and_exact_channel_match(tmp_path):
    export(tmp_path / '.downloads' / 'chat [123].json', [msg('1')])
    export(tmp_path / 'chat [1234].json', [msg('1')], cid='1234')
    assert dce._files_for_channel(tmp_path, '123') == []
    assert len(dce.export_files(tmp_path)) == 1


def test_local_media_link_survives_move(tmp_path):
    media = tmp_path / 'media' / 'pic.png'
    media.parent.mkdir()
    media.write_bytes(b'pic')
    export(tmp_path / 'Guild - chat [123].json', [dict(msg('1'), attachments=[{'url':'media/pic.png'}])])
    result = consolidate(tmp_path, 'chat', {'id':'123'})
    target = tmp_path / result['path']
    link = json.loads(target.read_text())['messages'][0]['attachments'][0]['url']
    assert (target.parent / link).resolve() == media


def test_archive_lock(tmp_path):
    with archive_lock(tmp_path):
        with pytest.raises(RuntimeError):
            with archive_lock(tmp_path):
                pass


def test_authenticated_http(tmp_path):
    config = tmp_path / 'channels.yaml'
    config.write_text('output_dir: exports\nchannels: {chat: {id: "123"}}')
    app = Dashboard(config)
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app, 'secret'))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    try:
        with urllib.request.urlopen(url) as response:
            assert b'History worth keeping' in response.read()
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(url + '/api/state')
        assert error.value.code == 403
        request = urllib.request.Request(url + '/api/state', headers={'X-DCE-Key':'secret'})
        with urllib.request.urlopen(request) as response:
            assert json.load(response)['channels'][0]['name'] == 'chat'
        request = urllib.request.Request(url + '/api/start', data=b'{}', method='POST')
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_sync_fake_exporter_same_day(tmp_path, monkeypatch):
    config = tmp_path / 'channels.yaml'
    config.write_text('output_dir: exports\nchannels: {chat: {id: "123"}}')
    output = tmp_path / 'exports'
    export(output / f'chat [123] (pulled {date.today()}).json', [msg('1')])
    binary = tmp_path / 'exporter'
    binary.write_text('''#!/usr/bin/env python3
import json, sys
from pathlib import Path
out = Path(sys.argv[sys.argv.index('-o')+1])
print('50% token-secret', flush=True)
(out / 'chat [123].json').write_text(json.dumps({'channel':{'id':'123'}, 'messages':[{'id':'2','timestamp':'2026-08-02'}]}))
''')
    binary.chmod(0o755)
    monkeypatch.setattr(dce, 'load_token', lambda _: 'token-secret')
    monkeypatch.setattr(dce, 'find_dce_binary', lambda: str(binary))
    app = Dashboard(config)
    app.run('sync', {'chat'})
    state = app.snapshot()
    assert state['channels'][0]['status'] == 'done'
    assert state['channels'][0]['messages'] == 2
    assert 'token-secret' not in json.dumps(state)
    assert len(dce._files_for_channel(output, '123')) == 1


def test_failed_export_never_advances_archive(tmp_path, monkeypatch):
    config = tmp_path / 'channels.yaml'
    config.write_text('output_dir: exports\nchannels: {chat: {id: "123"}}')
    old = export(tmp_path / 'exports/chat [123] (pulled 2026-08-01).json', [msg('1')])
    before = old.read_bytes()
    monkeypatch.setattr(dce, 'load_token', lambda _: 'secret')
    monkeypatch.setattr(dce, 'find_dce_binary', lambda: 'fake')
    app = Dashboard(config)
    def fail(*args):
        raise RuntimeError('network failure secret')
    monkeypatch.setattr(app, 'export', fail)
    app.run('sync', {'chat'})
    assert old.read_bytes() == before
    assert app.snapshot()['channels'][0]['status'] == 'error'
    assert 'secret' not in json.dumps(app.snapshot())
    assert dce.parse_last_after(tmp_path / 'exports', '123') == date(2026, 8, 1)


def test_cancel_terminates_exporter_and_keeps_archive(tmp_path, monkeypatch):
    import time
    config = tmp_path / 'channels.yaml'
    config.write_text('output_dir: exports\nchannels: {chat: {id: "123"}}')
    old = export(tmp_path / 'exports/chat [123].json', [msg('1')])
    binary = tmp_path / 'exporter'
    binary.write_text('#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n')
    binary.chmod(0o755)
    monkeypatch.setattr(dce, 'load_token', lambda _: 'secret')
    monkeypatch.setattr(dce, 'find_dce_binary', lambda: str(binary))
    app = Dashboard(config)
    app.start('sync', ['chat'])
    deadline = time.monotonic() + 5
    while app.snapshot()['channels'][0]['status'] != 'downloading' and time.monotonic() < deadline:
        time.sleep(.01)
    app.cancel.set()
    while app.running and time.monotonic() < deadline:
        time.sleep(.01)
    assert not app.running
    assert app.snapshot()['channels'][0]['status'] == 'cancelled'
    assert old.exists()


def test_legacy_merge_target_keeps_latest_pulled_date(tmp_path):
    first = tmp_path / 'chat [123] (after 2026-08-01) (pulled 2026-08-02).json'
    second = tmp_path / 'messages [123] (pulled 2026-08-03).json'
    assert dce._pick_merge_target([first, second]) == second
