import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

import dce_discovery
import dce_settings
import dce_sync
from dce_dashboard import Dashboard


def app_fixture(tmp_path, count=1):
    config=tmp_path/'channels.yaml'
    config.write_text('output_dir: exports\nchannels:\n'+''.join(f'  c{i}: {{id: "{i+100}"}}\n' for i in range(count)))
    return Dashboard(config)


def test_settings_validate_and_persist(tmp_path):
    app=app_fixture(tmp_path)
    values=dict(dce_settings.DEFAULTS,jobs=6,retries=4,media=True,utc=True,threads='Active')
    result=app.save_settings(values)
    assert result['options']==values
    assert dce_settings.load(app.config)==values
    for bad in [dict(jobs=0),dict(jobs=7),dict(jobs=True),dict(retries=-1),dict(media='yes'),dict(threads='all'),dict(token='secret')]:
        with pytest.raises(ValueError):app.save_settings(bad)
    app.running=True
    with pytest.raises(ValueError):app.save_settings(values)


def test_token_is_atomic_private_and_never_returned(tmp_path,monkeypatch):
    monkeypatch.delenv('DCE_TOKEN',raising=False)
    path=tmp_path/'private/token'
    monkeypatch.setattr(dce_sync,'TOKEN_FILE',path)
    app=app_fixture(tmp_path)
    token='secret-placeholder-token-1234'
    response=app.save_token(token)
    assert path.read_text()==token
    assert path.stat().st_mode & 0o777 == 0o600
    assert token not in json.dumps(response)
    assert response['token_present']
    with pytest.raises(ValueError):app.save_token('bad token with spaces')
    monkeypatch.setenv('DCE_TOKEN','environment-token')
    with pytest.raises(ValueError,match='environment'):app.save_token(token)


def test_parallel_downloads_and_serial_merges(tmp_path,monkeypatch):
    import dce_dashboard
    app=app_fixture(tmp_path,4)
    app.options['jobs']=2
    monkeypatch.setattr(dce_sync,'load_token',lambda _: 'secret')
    monkeypatch.setattr(dce_sync,'find_dce_binary',lambda:'fake')
    counters=dict(downloads=0,max_downloads=0,merges=0,max_merges=0)
    lock=threading.Lock()
    def export(row,token,binary,stage):
        with lock:
            counters['downloads']+=1
            counters['max_downloads']=max(counters['max_downloads'],counters['downloads'])
        time.sleep(.05)
        (stage/f'channel [{row["id"]}].json').write_text(json.dumps({'channel':{'id':row['id']},'messages':[]}))
        with lock:counters['downloads']-=1
        return True
    original=dce_dashboard.consolidate
    def merge(*args,**kwargs):
        with lock:
            counters['merges']+=1
            counters['max_merges']=max(counters['max_merges'],counters['merges'])
        time.sleep(.02)
        result=original(*args,**kwargs)
        with lock:counters['merges']-=1
        return result
    monkeypatch.setattr(app,'export',export)
    monkeypatch.setattr(dce_dashboard,'consolidate',merge)
    app.run('sync',{r['name'] for r in app.rows})
    assert counters['max_downloads']==2
    assert counters['max_merges']==1
    assert all(r['status']=='done' for r in app.rows)


def test_export_settings_reach_engine_and_media_survives_merge(tmp_path,monkeypatch):
    app=app_fixture(tmp_path)
    app.options.update(media=True,reuse_media=True,utc=True,markdown=False,full_history=True)
    binary=tmp_path/'fake-exporter'
    binary.write_text('''#!/usr/bin/env python3
import sys,os,json
from pathlib import Path
assert '-t' not in sys.argv
assert os.environ['DISCORD_TOKEN']=='private-placeholder'
assert '--after' not in sys.argv
assert sys.argv[sys.argv.index('--utc')+1]=='true'
assert sys.argv[sys.argv.index('--markdown')+1]=='false'
assert sys.argv[sys.argv.index('--respect-rate-limits')+1]=='true'
media=Path(sys.argv[sys.argv.index('--media-dir')+1]);media.mkdir(parents=True,exist_ok=True)
(media/'image.png').write_bytes(b'asset')
out=Path(sys.argv[sys.argv.index('-o')+1])
(out/'channel [100].json').write_text(json.dumps({'channel':{'id':'100'},'messages':[{'id':'1','attachments':[{'url':str(media/'image.png')}]}]}))
''')
    binary.chmod(0o755)
    monkeypatch.setattr(dce_sync,'load_token',lambda _:'private-placeholder')
    monkeypatch.setattr(dce_sync,'find_dce_binary',lambda:str(binary))
    app.run('sync',{'c0'})
    assert app.rows[0]['status']=='done'
    target=dce_sync._files_for_channel(app.output,'100')[0]
    asset=json.loads(target.read_text())['messages'][0]['attachments'][0]['url']
    assert (target.parent/asset).resolve().read_bytes()==b'asset'


def test_metadata_uses_stdin_not_command_line(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil,'which',lambda _:'/usr/bin/curl')
    def run(command,**kwargs):
        assert 'private-token' not in ' '.join(command)
        assert 'private-token' in kwargs['input']
        return SimpleNamespace(returncode=0,stdout='[{"id":"10","icon":"'+('a'*32)+'"}]\n200')
    monkeypatch.setattr(dce_discovery.subprocess,'run',run)
    assert dce_discovery.guild_icons('private-token')['10'].endswith('size=64')


def test_metadata_does_not_retry_rate_limits(monkeypatch):
    calls=[]
    def page(*args):
        calls.append(args)
        return 429,None
    monkeypatch.setattr(dce_discovery,'_metadata_page',page)
    assert dce_discovery.guild_icons('token')=={}
    assert len(calls)==1
