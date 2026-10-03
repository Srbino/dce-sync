import json
import threading
from pathlib import Path
from types import SimpleNamespace
import plistlib

import pytest
import yaml

import dce_storage as storage
import dce_schedule as schedule
import dce_sync as dce
from dce_archive import consolidate
from dce_dashboard import Dashboard


def make_export(path, cid='123', content='message'):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(dict(guild=dict(id='5',name='Guild'),channel=dict(id=cid,name='builds',category='Guides'),messages=[dict(id='1',content=content,attachments=[dict(url='media/pic.png')])])) )
    return path


def test_inventory_includes_untracked_and_category_merge_preserves_media(tmp_path):
    first = make_export(tmp_path/'Guild - builds [123].json')
    second = make_export(tmp_path/'Guild - builds [123] (pulled 2026-01-01).json',content='updated')
    (tmp_path/'media').mkdir();(tmp_path/'media/pic.png').write_bytes(b'pic')
    plan=storage.inventory(tmp_path,{})
    assert plan['files']==2 and not plan['channels'][0]['tracked']
    row=plan['channels'][0]
    result=consolidate(tmp_path,row['name'],row,layout='server_category')
    target=tmp_path/result['path']
    assert target.parent.parent.name=='Guides'
    msg=json.loads(target.read_text())['messages'][0]
    assert msg['content']=='updated'
    assert (target.parent/msg['attachments'][0]['url']).read_bytes()==b'pic'
    assert not first.exists() and not second.exists()
    moved=consolidate(tmp_path,row['name'],row,layout='server')
    assert (tmp_path/moved['path']).parent.parent.name=='Guild'
    assert not (tmp_path/'archive/Guild/Guides').exists()


def test_relocation_keeps_source_comments_and_integrity(tmp_path):
    config=tmp_path/'channels.yaml';config.write_text('# custom note\noutput_dir: exports\nchannels: {}\n')
    output=tmp_path/'exports';source=make_export(output/'channel [123].json')
    target=tmp_path/'New archive'
    cfg=storage.relocate(output,config,yaml.safe_load(config.read_text()),target,True,threading.Event(),lambda *a:None)
    assert source.read_bytes()==(target/source.name).read_bytes()
    assert '# custom note' in config.read_text()
    assert cfg['output_dir']==str(target)
    assert dce.output_dir_from_cfg(dce.load_config(config),config)==target
    for bad in [output,output/'nested',tmp_path,target]:
        with pytest.raises(ValueError):storage.validate_destination(output,str(bad))


def test_relocation_cancellation_keeps_original_active(tmp_path):
    config=tmp_path/'channels.yaml';config.write_text('output_dir: exports\nchannels: {}\n')
    output=tmp_path/'exports';make_export(output/'channel [123].json')
    stop=threading.Event();stop.set()
    before=config.read_bytes()
    with pytest.raises(InterruptedError):storage.relocate(output,config,yaml.safe_load(before),tmp_path/'new',True,stop,lambda *a:None)
    assert config.read_bytes()==before
    assert not list((tmp_path/'new').rglob('*.json'))


def test_report_migration_retains_local_linked_html(tmp_path):
    channels=[dict(id='123',display_name='builds',server='Guild',category='Guides')]
    root=tmp_path/'reports/2026-01-01-123-abcdef';root.mkdir(parents=True)
    (root/'index.html').write_text('<img src="media/a.png">')
    (root/'media').mkdir();(root/'media/a.png').write_bytes(b'a')
    local=tmp_path/'local [123].html';local.write_text('<img src="local.png">')
    remote=tmp_path/'remote [123].html';remote.write_text('<img src="https://example.com/image.png">')
    assert storage.organize_reports(tmp_path,channels)==2
    assert local.exists() and not remote.exists()
    assert list(storage.report_folder(tmp_path,channels[0]).rglob('a.png'))


def test_schedule_install_disable_and_rollback(tmp_path,monkeypatch):
    monkeypatch.setattr(schedule.sys,'platform','darwin')
    monkeypatch.setattr(schedule.Path,'home',classmethod(lambda cls:tmp_path))
    config=tmp_path/'channels.yaml';config.write_text('channels: {}')
    calls=[]
    def run(command,**kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1 if command[1]=='print' else 0)
    monkeypatch.setattr(schedule.subprocess,'run',run)
    values=dict(enabled=True,time='03:25',channels=['chat'])
    result=schedule.save(config,values,{'chat'})
    assert result['enabled'] and result['installed']
    agent=schedule.paths(config)[2]
    definition=plistlib.loads(agent.read_bytes())
    assert definition['StartCalendarInterval']==dict(Hour=3,Minute=25)
    assert definition['ProgramArguments'][-1]==str(config)
    assert 'TOKEN' not in str(definition)
    assert any(c[1]=='bootstrap' for c in calls)
    def fail(command,**kwargs):return SimpleNamespace(returncode=1)
    monkeypatch.setattr(schedule.subprocess,'run',fail)
    with pytest.raises(ValueError):schedule.save(config,dict(values,time='04:00'),{'chat'})
    assert schedule.load(config)['time']=='03:25'
    monkeypatch.setattr(schedule.subprocess,'run',run)
    assert not schedule.save(config,dict(values,enabled=False),{'chat'})['enabled']
    assert not agent.exists()


def test_schedule_rejects_unknown_channels_and_bad_times():
    for value in [dict(enabled=True,time='03:00',channels=[]),dict(enabled=True,time='24:00',channels=['a']),dict(enabled=True,time='03:00',channels=['other'])]:
        with pytest.raises(ValueError):schedule.validate(value,{'a'})


def test_unreadable_exports_are_retained_without_blocking_merge(tmp_path):
    good=make_export(tmp_path/'chat [123] (pulled 2026-01-01).json')
    bad=make_export(tmp_path/'chat [123].json');bad.write_text(bad.read_text()[:-3])
    original=bad.read_bytes()
    isolated=storage.isolate_invalid(tmp_path,threading.Event(),lambda *a:None)
    assert len(isolated)==1
    assert (tmp_path/isolated[0]).read_bytes()==original
    result=consolidate(tmp_path,'chat',{'id':'123'},layout='server_category')
    assert result['messages']==1
    assert not good.exists() and not bad.exists()


def test_scheduled_worker_runs_selected_channels_and_records_result(tmp_path,monkeypatch):
    config=tmp_path/'channels.yaml';config.write_text('output_dir: exports\nchannels: {chosen: {id: "123"}, other: {id: "456"}}')
    prefs=schedule.paths(config)[0]
    prefs.write_text('enabled: true\ntime: "03:00"\nchannels: [chosen]\n')
    binary=tmp_path/'exporter'
    binary.write_text('''#!/usr/bin/env python3
import json,sys
from pathlib import Path
cid=sys.argv[sys.argv.index('-c')+1]
assert cid=='123'
out=Path(sys.argv[sys.argv.index('-o')+1])
(out/f'channel [{cid}].json').write_text(json.dumps({'channel':{'id':cid},'messages':[]}))
''')
    binary.chmod(0o755)
    monkeypatch.setattr(dce,'load_token',lambda _: 'placeholder-token')
    monkeypatch.setattr(dce,'find_dce_binary',lambda:str(binary))
    monkeypatch.setattr(schedule,'run_in_open_app',lambda *a:None)
    assert schedule.run(config)==0
    result=schedule.load(config)['last']
    assert result['status']=='completed' and result['channels']==['chosen']
    assert len(dce.export_files(tmp_path/'exports'))==1
    assert 'placeholder-token' not in json.dumps(result)
