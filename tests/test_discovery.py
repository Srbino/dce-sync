import subprocess
import time
from types import SimpleNamespace

import pytest
import yaml

import dce_discovery as discovery
from dce_dashboard import Dashboard


def fixture_app(tmp_path, flow=False):
    config = tmp_path / 'channels.yaml'
    config.write_text('output_dir: exports\nchannels: {}\n' if flow else
                      '# Keep this note\noutput_dir: exports\nchannels:\n  existing: {id: "123", server: Alpha}\n# Historic channels must stay documented\n')
    app = Dashboard(config)
    app.catalog['guilds'] = [{'id':'10','name':'Alpha','icon_url':None}, {'id':'20','name':'Beta'}]
    app.catalog['channels'] = {'10':[{'id':'123','name':'general','category':'Chat'},
                                     {'id':'456','name':'news','category':'Updates'},
                                     {'id':'457','name':'news','category':'Chat'}],
                                '20':[{'id':'789','name':'general','category':'Chat'}]}
    return app


def test_parse_cli_listing_preserves_unicode_names():
    rows = discovery.parse_listing('\x1b[32m123456789012345678 | ✨ Community / chat-with-friends\x1b[0m\n987654321098765432 | A | B\n')
    assert rows[0]['name'] == '✨ Community / chat-with-friends'
    assert rows[1]['name'] == 'A | B'
    with pytest.raises(ValueError):
        discovery.parse_listing('Unexpected output format')


def test_discovery_uses_environment_and_parses_categories(monkeypatch):
    monkeypatch.setattr(discovery.dce, 'load_token', lambda _: 'private-token')
    monkeypatch.setattr(discovery.dce, 'find_dce_binary', lambda: '/fake/dce')
    def run(command, **kwargs):
        assert 'private-token' not in command
        assert kwargs['env']['DISCORD_TOKEN'] == 'private-token'
        return SimpleNamespace(returncode=0, stdout='123456789012345678 | Community / general\n', stderr='')
    monkeypatch.setattr(discovery.subprocess, 'run', run)
    assert discovery.discover('10') == [{'id':'123456789012345678','name':'general','category':'Community'}]


def test_discovery_redacts_errors_and_timeout(monkeypatch):
    monkeypatch.setattr(discovery.dce, 'load_token', lambda _: 'private-token')
    monkeypatch.setattr(discovery.dce, 'find_dce_binary', lambda: '/fake/dce')
    monkeypatch.setattr(discovery.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1,stdout='',stderr='401 private-token'))
    with pytest.raises(ValueError) as error:
        discovery.discover()
    assert 'private-token' not in str(error.value)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('dce',75)
    monkeypatch.setattr(discovery.subprocess, 'run', timeout)
    with pytest.raises(ValueError,match='too long'):
        discovery.discover()


def test_add_preserves_comments_avoids_duplicates_and_collisions(tmp_path):
    app = fixture_app(tmp_path)
    result = app.add_channels([{'guild':'10','id':'123'}, {'guild':'10','id':'456'},
                               {'guild':'10','id':'457'}, {'guild':'10','id':'456'}])
    assert result['added'] == 2
    assert result['channels'] == ['existing','alpha-news','alpha-news-2']
    text = app.config.read_text()
    assert '# Keep this note' in text and '# Historic channels must stay documented' in text
    assert yaml.safe_load(text)['channels']['alpha-news']['guild_id'] == '10'
    assert app.config.with_name('channels.yaml.before-dashboard').exists()
    assert len(Dashboard(app.config).rows) == 3
    assert app.add_channels([{'guild':'10','id':'456'}])['added'] == 0
    snapshot=app.catalog_snapshot()
    assert snapshot['channels']['10'][1]['tracked']
    assert snapshot['guilds'][0]['tracked'] == 3


def test_add_and_sync_includes_existing_channels(tmp_path, monkeypatch):
    app = fixture_app(tmp_path)
    starts=[]
    monkeypatch.setattr(app,'start',lambda operation,names:starts.append((operation,names)))
    app.add_channels([{'guild':'10','id':'123'},{'guild':'20','id':'789'}],sync=True)
    assert starts == [('sync',['existing','beta-general'])]


def test_add_rejects_untrusted_ids_external_edits_and_running_sync(tmp_path):
    app=fixture_app(tmp_path)
    before=app.config.read_bytes()
    with pytest.raises(ValueError):
        app.add_channels([{'guild':'20','id':'456'}])
    assert app.config.read_bytes()==before
    app.running=True
    with pytest.raises(ValueError,match='current sync'):
        app.add_channels([{'guild':'10','id':'456'}])
    app.running=False
    app.config.write_text(app.config.read_text()+'priority_server: Alpha\n')
    with pytest.raises(ValueError,match='outside'):
        app.add_channels([{'guild':'10','id':'456'}])


def test_empty_registry_bootstrap_and_flow_yaml(tmp_path):
    for folder in ['missing','flow']:
        root=tmp_path/folder
        root.mkdir()
        app=fixture_app(root,flow=True)
        if folder=='missing':
            app.config.unlink()
            new=Dashboard(app.config)
            new.catalog=app.catalog
            app=new
        assert app.add_channels([{'guild':'10','id':'456'}])['added']==1
        assert yaml.safe_load(app.config.read_text())['channels']['alpha-news']['id']=='456'


def test_catalog_discovery_is_async_and_unknown_server_rejected(tmp_path, monkeypatch):
    import dce_dashboard
    app=fixture_app(tmp_path)
    with pytest.raises(ValueError):
        app.load_catalog('unknown')
    monkeypatch.setattr(dce_dashboard,'discover',lambda guild, threads:[{'id':'30','name':'Gamma'}])
    app.load_catalog(refresh=True)
    deadline=time.monotonic()+2
    while app.catalog_snapshot()['loading'] and time.monotonic()<deadline:
        time.sleep(.01)
    assert app.catalog_snapshot()['guilds'][0]['name']=='Gamma'


def test_icon_lookup_has_safe_fallback(monkeypatch):
    import urllib.request
    def fail(*args, **kwargs):
        raise OSError('offline')
    monkeypatch.setattr(urllib.request,'urlopen',fail)
    assert discovery.guild_icons('secret') == {}


def test_thread_listing_keeps_parent_and_archive_status():
    rows=discovery.parse_listing('123 | Projects / forum\n * 456 | Thread / A discussion | Archived\n * 789 | Thread / Another discussion | Active\n')
    assert rows[1]['kind']=='thread'
    assert rows[1]['parent_id']=='123'
    assert rows[1]['archived_thread'] is True
    assert rows[2]['parent_name']=='Projects / forum'


def test_icon_fallback_reads_export_header(tmp_path):
    import json
    icon='a' * 32
    (tmp_path/'chat [123].json').write_text(json.dumps({'guild':{'id':'10','iconUrl':f'https://cdn.discordapp.com/icons/10/{icon}.png?size=512'}, 'messages':[]}))
    assert discovery.archived_icons(tmp_path)['10'].endswith('.png?size=64')


def test_registry_with_four_space_indent(tmp_path):
    app=fixture_app(tmp_path)
    app.config.write_text(app.config.read_text().replace('  existing:', '    existing:'))
    assert app.add_channels([{'guild':'10','id':'456'}])['added']==1
    assert yaml.safe_load(app.config.read_text())['channels']['alpha-news']['id']=='456'
