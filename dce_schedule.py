"""Per-workspace daily sync, driven by the macOS user service manager."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
from datetime import datetime

import yaml
import dce_settings


def paths(config):
    ident = hashlib.sha256(str(config.resolve()).encode()).hexdigest()[:20]
    label = 'io.github.srbino.discord-archive.' + ident
    return (config.with_name(config.name + '.schedule.yaml'),
            config.with_name(config.name + '.schedule-state'),
            Path.home() / 'Library/LaunchAgents' / (label + '.plist'), label)


def load(config):
    prefs, state, agent, label = paths(config)
    values = yaml.safe_load(prefs.read_text()) if prefs.exists() else dict(enabled=False, time='03:00', channels=[])
    last = json.loads(state.read_text()) if state.exists() else None
    return dict(**values, last=last, supported=sys.platform == 'darwin', installed=agent.exists(),
                timezone=datetime.now().astimezone().tzname(), log=str(config.with_name(config.name + '.schedule.log')))


def validate(values, names):
    if not isinstance(values, dict) or set(values) != {'enabled', 'time', 'channels'}:
        raise ValueError('Invalid schedule settings.')
    if type(values['enabled']) is not bool or not isinstance(values['time'], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', values['time']):
        raise ValueError('Choose a daily time in HH:MM format.')
    channels = values['channels']
    if not isinstance(channels, list) or not all(isinstance(n, str) and n in names for n in channels):
        raise ValueError('Select existing tracked channels for the schedule.')
    if values['enabled'] and not channels:
        raise ValueError('Select at least one channel for the daily sync.')
    return dict(enabled=values['enabled'], time=values['time'], channels=list(dict.fromkeys(channels)))


def agent_definition(config, values):
    _, _, _, label = paths(config)
    hour, minute = map(int, values['time'].split(':'))
    log = str(config.with_name(config.name + '.schedule.log'))
    return dict(Label=label, ProgramArguments=[sys.executable, str(Path(__file__).resolve()), '--run', str(config.resolve())],
                StartCalendarInterval=dict(Hour=hour, Minute=minute), ProcessType='Background',
                WorkingDirectory=str(config.parent.resolve()), StandardOutPath=log, StandardErrorPath=log,
                EnvironmentVariables=dict(PATH=os.pathsep.join([str(Path.home()/'.local/bin'),'/opt/homebrew/bin','/usr/local/bin','/usr/bin','/bin'])))


def save(config, values, names):
    values = validate(values, names)
    if sys.platform != 'darwin':
        raise ValueError('The built-in daily scheduler currently supports macOS. Use the CLI with your system scheduler on Linux.')
    prefs, _, agent, label = paths(config)
    domain = f'gui/{os.getuid()}'
    old_agent = agent.read_bytes() if agent.exists() else None
    old_prefs = prefs.read_text() if prefs.exists() else None
    loaded = subprocess.run(['launchctl','print',domain+'/'+label], capture_output=True).returncode == 0
    if loaded:
        result = subprocess.run(['launchctl','bootout',domain+'/'+label], capture_output=True, text=True)
        if result.returncode: raise ValueError('Could not stop the previous schedule. No settings were changed.')
    try:
        dce_settings.atomic_write(prefs, yaml.safe_dump(values, sort_keys=False))
        if values['enabled']:
            agent.parent.mkdir(parents=True, exist_ok=True)
            dce_settings.atomic_write(agent, plistlib.dumps(agent_definition(config, values)).decode())
            result = subprocess.run(['launchctl','bootstrap',domain,str(agent)], capture_output=True, text=True)
            if result.returncode:
                raise ValueError('macOS could not enable the schedule. Check Background Items permissions and the workspace location.')
        else:
            agent.unlink(missing_ok=True)
    except Exception:
        if old_prefs is None: prefs.unlink(missing_ok=True)
        else: dce_settings.atomic_write(prefs, old_prefs)
        if old_agent is None: agent.unlink(missing_ok=True)
        else:
            dce_settings.atomic_write(agent, old_agent.decode())
            if loaded: subprocess.run(['launchctl','bootstrap',domain,str(agent)], capture_output=True)
        raise
    return load(config)


def run(config):
    from dce_dashboard import Dashboard
    # A disconnected external drive must not create a replacement workspace.
    if not config.is_file():
        print('Scheduled sync skipped: workspace is unavailable.', flush=True)
        return 1
    options = load(config)
    if not options['enabled']: return 0
    app = Dashboard(config)
    validate({key:options[key] for key in ('enabled','time','channels')}, {r['name'] for r in app.rows})
    state_path = paths(config)[1]
    result = dict(started=datetime.now().astimezone().isoformat(), status='running', channels=options['channels'])
    dce_settings.atomic_write(state_path, json.dumps(result))
    remote = run_in_open_app(config, options['channels'])
    if remote is None:
        app.run('sync', set(options['channels']))
        remote = app.snapshot()
    errors = [dict(channel=r['name'], error=r['detail']) for r in remote['channels'] if r['status']=='error' and r['name'] in options['channels']]
    result.update(finished=datetime.now().astimezone().isoformat(), status='error' if remote.get('error') or errors else 'completed', error=remote.get('error'), failures=errors)
    dce_settings.atomic_write(state_path, json.dumps(result))
    print(json.dumps(result), flush=True)
    return int(result['status']=='error')


def run_in_open_app(config, channels):
    """Show scheduled progress in an open dashboard; otherwise run headlessly."""
    import time
    import urllib.error
    import urllib.request
    from urllib.parse import urlparse
    ident = hashlib.sha256(str(config.resolve()).encode()).hexdigest()[:20]
    instance = Path.home()/'.cache/dce-sync'/(ident+'.app')
    if not instance.exists(): return None
    url = urlparse(instance.read_text().strip())
    if url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port or not url.fragment:
        return None
    def request(route, body=None):
        req = urllib.request.Request(f'http://127.0.0.1:{url.port}/api/'+route,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'X-DCE-Key':url.fragment,'Content-Type':'application/json'})
        return json.load(urllib.request.urlopen(req,timeout=10))
    try:
        state = request('state')
    except (OSError, ValueError): return None
    if state['running']:
        return dict(channels=[], error='Scheduled sync skipped: another operation is running.')
    try:
        request('start',dict(operation='sync',channels=channels))
        while True:
            state = request('state')
            if not state['running']: return state
            time.sleep(1)
    except (OSError, ValueError):
        # Do not start a second writer when the accepted request loses contact.
        return dict(channels=[], error='Scheduled sync lost contact with the dashboard; inspect its current run.')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.run.resolve()))
