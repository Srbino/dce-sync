"""Local-only dashboard. No web framework or external assets required."""
from __future__ import annotations

import argparse
import copy
import concurrent.futures
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import tempfile
import time
import webbrowser
from collections import deque
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import dce_sync as dce
from dce_archive import archive_lock, consolidate, server_name
from dce_discovery import discover, archived_icons
import dce_settings
import dce_exports
import dce_versions
import dce_storage
import dce_schedule


class Dashboard:
    def __init__(self, config: Path):
        self.config = config
        self.cfg = dce.load_config(config) if config.exists() else {'output_dir': 'exports', 'channels': {}}
        self.output = dce.output_dir_from_cfg(self.cfg, config)
        self.options = dce_settings.load(config)
        self.merge_lock = threading.Lock()
        self.engine = {'installed': False, 'version': 'Checking…', 'path': None}
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.running = False
        self.operation = None
        self.export_options = None
        self.version_status = None
        self.maintenance = None
        self.error = None
        self.logs = deque(maxlen=200)
        self.catalog = dict(guilds=None, channels={}, threadModes={}, loading=False, loadingGuild=None, error=None)
        self.rows = []
        seen = set()
        for name, channel in (self.cfg.get('channels') or {}).items():
            cid = str(channel['id'])
            if not cid.isdigit() or cid in seen:
                raise ValueError('Every channel must have a unique numeric ID.')
            seen.add(cid)
            exports = dce._files_for_channel(self.output, cid)
            last = dce.parse_last_after(self.output, cid)
            self.rows.append(dict(name=name, display_name=channel.get('display_name', name), id=cid, server=server_name(self.output, name, channel),
                                  status='idle', last=last.isoformat() if last else None,
                                  files=len(exports), bytes=sum(f.stat().st_size for f in exports),
                                  downloaded=0, percent=None, messages=None, duplicates=0,
                                  detail='Ready', started=None, elapsed=0))
        self.rows.sort(key=lambda r: (r['server'] != self.cfg.get('priority_server'), r['server'], r['name']))
        self.refresh_rows()
        threading.Thread(target=self.check_engine, daemon=True).start()

    def check_engine(self):
        binary = shutil.which('discordchatexporter') or shutil.which('DiscordChatExporter.Cli')
        version = dce._installed_dce_version(binary) if binary else None
        with self.lock:
            self.engine = dict(installed=bool(binary), path=binary, version=version or 'Not installed')

    def settings_snapshot(self):
        token = dce._read_token_file(dce.TOKEN_FILE)
        source = 'environment' if os.environ.get('DCE_TOKEN') else 'saved file' if token else 'not configured'
        if source == 'not configured' and dce._read_token_file(Path.cwd() / '.dce_token'):
            source = 'workspace file'
        age = max(0, int((time.time() - dce.TOKEN_FILE.stat().st_mtime) / 86400)) if token else None
        with self.lock:
            return dict(options=dict(self.options), engine=dict(self.engine), token_present=source != 'not configured',
                        token_source=source, token_age_days=age, running=self.running,
                        output=str(self.output), config=str(self.config), tested_version='2.48',
                        schedule=dce_schedule.load(self.config))

    def library(self):
        return dict(dce_storage.inventory(self.output, self.cfg.get('channels') or {}), output=str(self.output), layout=self.options['layout'])

    def choose_folder(self):
        if os.sys.platform != 'darwin':
            raise ValueError('Enter the destination path below on this platform.')
        try:
            result = subprocess.run(['/usr/bin/osascript', '-e', 'POSIX path of (choose folder with prompt "Choose an empty folder for your Discord archive")'],
                                    capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            raise ValueError('Folder selection timed out. Try again or enter the path.') from None
        if result.returncode:
            return dict(path=None)
        return dict(path=result.stdout.strip())

    def save_schedule(self, values):
        with self.lock:
            if self.running:
                raise ValueError('Wait for the current operation before changing the schedule.')
            with archive_lock(self.output):
                return dce_schedule.save(self.config, values, {r['name'] for r in self.rows})

    def change_storage(self, value, copy_existing):
        if type(copy_existing) is not bool:
            raise ValueError('Choose whether to copy the existing archive.')
        with self.lock:
            if self.running:
                raise ValueError('Wait for the current operation before changing folders.')
            destination = dce_storage.validate_destination(self.output, value)
            self.running, self.operation, self.error = True, 'relocate', None
            self.cancel.clear()
            def worker():
                try:
                    with archive_lock(self.output):
                        self.maintenance = 'Copying and verifying archive…'
                        cfg = dce_storage.relocate(self.output, self.config, self.cfg, destination, copy_existing, self.cancel,
                            lambda n,total: setattr(self, 'maintenance', f'Copying and verifying {n}/{total} files'))
                        with self.lock:
                            self.cfg, self.output = cfg, destination
                            self.refresh_rows()
                    self.maintenance = 'Archive folder updated. The original folder and its version backups were retained.'
                except Exception as exc:
                    self.error = str(exc)
                    self.maintenance = 'Folder change failed; original archive remains active.'
                finally:
                    self.running = False
            threading.Thread(target=worker, daemon=True).start()

    def refresh_rows(self):
        for row in self.rows:
            paths = dce._files_for_channel(self.output, row['id'])
            last = dce.parse_last_after(self.output, row['id'])
            row.update(files=len(paths), bytes=sum(p.stat().st_size for p in paths), last=last.isoformat() if last else None)
            if paths:
                try:
                    channel = dce_storage.metadata(paths[-1]).get('channel', {})
                    row.update(category=channel.get('category') or 'Uncategorized', display_name=channel.get('name') or row['display_name'])
                except (ValueError, OSError):
                    pass

    def open_folder(self, location='archive', channel=None):
        if location not in ('archive', 'reports', 'versions'):
            raise ValueError('Unknown archive location.')
        folder = {'archive': self.output, 'reports': self.output / 'reports',
                  'versions': self.output.parent / (self.output.name + '-versions')}[location]
        if channel is not None:
            row = next((r for r in self.rows if r['name'] == channel), None)
            if row is None:
                raise ValueError('Unknown channel.')
            exports = dce._files_for_channel(self.output, row['id'])
            if not exports:
                raise ValueError('This channel has not been downloaded yet.')
            folder = exports[0].parent
        folder.mkdir(parents=True, exist_ok=True)
        opener = '/usr/bin/open' if os.sys.platform == 'darwin' else shutil.which('xdg-open')
        if not opener:
            raise ValueError(f'No desktop folder opener is available. Archive location: {folder}')
        try:
            result = subprocess.run([opener, str(folder)], capture_output=True, text=True, timeout=8)
        except subprocess.TimeoutExpired:
            raise ValueError(f'The file manager did not respond. Open this folder manually: {folder}') from None
        if result.returncode:
            raise ValueError(f'Could not open the file manager. Archive location: {folder}')
        return {'ok': True, 'path': str(folder)}

    def save_settings(self, values):
        with self.lock:
            if self.running:
                raise ValueError('Stop the current sync before changing settings.')
            self.options = dce_settings.save(self.config, values)
        return self.settings_snapshot()

    def save_token(self, token):
        if not isinstance(token, str) or not 16 <= len(token.strip()) <= 4096 or any(c.isspace() for c in token.strip()):
            raise ValueError('Paste the Authorization token only, without a header name or spaces.')
        with self.lock:
            if self.running or self.catalog['loading']:
                raise ValueError('Wait for the current operation before changing the token.')
            if os.environ.get('DCE_TOKEN'):
                raise ValueError('DCE_TOKEN is set in the environment. Update it there and restart the app.')
            dce_settings.atomic_write(dce.TOKEN_FILE, token.strip())
            self.catalog.update(guilds=None, channels={}, threadModes={}, error=None)
        return self.settings_snapshot()

    def catalog_snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.catalog)
            tracked = {r['id']: r for r in self.rows}
            for channels in result['channels'].values():
                for channel in channels:
                    row = tracked.get(channel['id'])
                    channel.update(tracked=bool(row), status=row['status'] if row else 'idle',
                                   last=row['last'] if row else channel.get('last'),
                                   archived=bool(row['files']) if row else channel.get('archived', False))
            for guild in result['guilds'] or []:
                # Older registries have server names but no guild ID.
                guild['tracked'] = sum(
                    1 for name, channel in (self.cfg.get('channels') or {}).items()
                    if str(channel.get('guild_id', '')) == guild['id'] or
                    (not channel.get('guild_id') and any(r['name'] == name and r['server'] == guild['name'] for r in self.rows)))
            return result

    def load_catalog(self, guild=None, refresh=False, threads='None'):
        if threads not in ('None', 'Active', 'All'):
            raise ValueError('Invalid thread mode.')
        with self.lock:
            if guild is not None and (not isinstance(guild, str) or not any(
                    g['id'] == guild for g in self.catalog['guilds'] or [])):
                raise ValueError('Select a server from the loaded list first.')
            if self.catalog['loading']:
                raise ValueError('A Discord listing is already loading.')
            if not refresh and ((guild is None and self.catalog['guilds'] is not None) or
                                (guild is not None and guild in self.catalog['channels'] and self.catalog['threadModes'].get(guild, 'None') == threads)):
                return
            self.catalog.update(loading=True, loadingGuild=guild, error=None)
            threading.Thread(target=self._discover, args=(guild, threads), daemon=True).start()

    def _discover(self, guild, threads):
        try:
            rows = discover(guild, threads)
            if guild is None:
                icons = archived_icons(self.output)
                with self.lock:
                    icons.update({g['id']: g['icon_url'] for g in self.catalog['guilds'] or [] if g.get('icon_url')})
                for row in rows:
                    row['icon_url'] = row.get('icon_url') or icons.get(row['id'])
            if guild is not None:
                exports = dce.export_files(self.output)
                ids = set()
                for path in exports:
                    match = re.search(r'\[(\d+)\]', path.name)
                    if match:
                        ids.add(match[1])
                for row in rows:
                    row['archived'] = row['id'] in ids
                    last = dce.parse_last_after(self.output, row['id']) if row['archived'] else None
                    row['last'] = last.isoformat() if last else None
            with self.lock:
                if guild is None:
                    self.catalog['guilds'] = rows
                else:
                    self.catalog['channels'][guild] = rows
                    self.catalog['threadModes'][guild] = threads
        except Exception as exc:
            # discovery raises safe user-facing errors; unexpected errors stay private.
            with self.lock:
                self.catalog['error'] = str(exc) if isinstance(exc, ValueError) else 'Could not load the listing. Try again.'
        finally:
            with self.lock:
                self.catalog['loading'] = False

    def add_channels(self, choices, sync=False):
        """Accept only IDs from the server-side catalog; persist before starting."""
        if not isinstance(choices, list) or not choices or len(choices) > 1000:
            raise ValueError('Select the channels you want to add.')
        if not isinstance(sync, bool):
            raise ValueError('Invalid sync option.')
        with self.lock:
            if self.running:
                raise ValueError('Wait for the current sync to finish or stop it first.')
            allowed = {(g['id'], c['id']): (g, c)
                       for g in self.catalog['guilds'] or []
                       for c in self.catalog['channels'].get(g['id'], [])}
            picked = []
            for choice in choices:
                if not isinstance(choice, dict):
                    raise ValueError('Invalid channel selection.')
                guild, cid = choice.get('guild'), choice.get('id')
                if not isinstance(guild, str) or not isinstance(cid, str) or (guild, cid) not in allowed:
                    raise ValueError('This channel is not in the loaded catalog. Refresh the Discord listing.')
                if (guild, cid) not in picked:
                    picked.append((guild, cid))
            # A config lock plus byte comparison prevents overwriting external edits.
            with archive_lock(self.output):
                original = self.config.read_bytes() if self.config.exists() else b''
                cfg = dce.load_config(self.config) if self.config.exists() else copy.deepcopy(self.cfg)
                if cfg != self.cfg:
                    raise ValueError('The configuration changed outside the app. Restart the app to load those changes.')
                cfg = copy.deepcopy(cfg)
                registry = cfg.setdefault('channels', {})
                by_id = {str(ch['id']): name for name, ch in registry.items()}
                added, names = [], []
                for guild, cid in picked:
                    server, channel = allowed[guild, cid]
                    name = by_id.get(cid)
                    if name is None:
                        base = dce._slugify(server['name']) + '-' + dce._slugify(channel['name'])
                        name, suffix = base, 2
                        while name in registry:
                            name = f'{base}-{suffix}'
                            suffix += 1
                        registry[name] = dict(id=cid, server=server['name'], guild_id=guild,
                                              display_name=channel['name'], category=channel['category'],
                                              icon_url=server.get('icon_url'), kind=channel.get('kind', 'channel'))
                        by_id[cid] = name
                        added.append(name)
                    names.append(name)
                if added:
                    # Append YAML entries in place, retaining comments and legacy archive notes.
                    # For unusual YAML layouts, refuse rather than silently erase comments.
                    import yaml
                    mapping = yaml.safe_dump({n: registry[n] for n in added}, sort_keys=False, allow_unicode=True)
                    text = original.decode('utf-8')
                    lines = text.splitlines(keepends=True)
                    start = next((i for i, line in enumerate(lines) if re.match(r'^channels:\s*(?:#.*)?$', line.rstrip('\r\n'))), None)
                    if start is not None:
                        end = next((i for i in range(start + 1, len(lines))
                                    if re.match(r'^[^\s#][^:]*:', lines[i])), len(lines))
                        indent = next((len(line) - len(line.lstrip()) for line in lines[start + 1:end]
                                       if line.strip() and not line.lstrip().startswith('#')), 2)
                        lines.insert(end, '\n' + ''.join(' ' * indent + line + '\n' for line in mapping.splitlines()))
                        updated = ''.join(lines)
                    else:
                        # Flow-style registries are supported too. Preserve the original as a backup.
                        updated = yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)
                    if yaml.safe_load(updated) != cfg:
                        raise ValueError('Could not safely update the configuration. No channels were changed.')
                    backup = self.config.with_name(self.config.name + '.before-dashboard')
                    if original and not backup.exists():
                        with backup.open('xb') as handle:
                            handle.write(original)
                    temporary = None
                    try:
                        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.config.parent,
                                                         delete=False, suffix='.tmp') as handle:
                            temporary = Path(handle.name)
                            handle.write(updated)
                            handle.flush()
                            os.fsync(handle.fileno())
                        if (self.config.read_bytes() if self.config.exists() else b'') != original:
                            raise ValueError('The configuration changed while saving. Try again.')
                        os.chmod(temporary, (self.config.stat().st_mode & 0o777) if self.config.exists() else 0o600)
                        os.replace(temporary, self.config)
                    finally:
                        if temporary:
                            temporary.unlink(missing_ok=True)
                    self.cfg = cfg
                    for name in added:
                        channel = registry[name]
                        exports = dce._files_for_channel(self.output, channel['id'])
                        last = dce.parse_last_after(self.output, channel['id'])
                        self.rows.append(dict(name=name, id=channel['id'], server=channel['server'],
                                              display_name=channel['display_name'], status='idle',
                                              last=last.isoformat() if last else None, files=len(exports),
                                              bytes=sum(p.stat().st_size for p in exports), downloaded=0,
                                              percent=None, messages=None, duplicates=0, detail='Ready',
                                              started=None, elapsed=0))
                    self.rows.sort(key=lambda r: (r['server'] != self.cfg.get('priority_server'), r['server'], r['name']))
            if sync:
                self.start('sync', names)
            return dict(added=len(added), channels=names)

    def snapshot(self):
        with self.lock:
            result = dict(running=self.running, operation=self.operation, error=self.error,
                          output=str(self.output), channels=copy.deepcopy(self.rows), logs=list(self.logs),
                          discovery_loading=self.catalog['loading'], jobs=self.options['jobs'],
                          version_status=self.version_status, maintenance=self.maintenance)
            icons = {g['name']: g.get('icon_url') for g in self.catalog['guilds'] or []}
            for row in result['channels']:
                row['server_icon'] = icons.get(row['server']) or self.cfg.get('channels', {}).get(row['name'], {}).get('icon_url')
        for row in result['channels']:
            if row['started'] and row['status'] in ('downloading', 'merging'):
                row['elapsed'] = round(time.monotonic() - row['started'])
        return result

    def update(self, row, **values):
        with self.lock:
            row.update(values)

    def log(self, name, message, token=''):
        if token:
            message = message.replace(token, '[redacted token]')
        message = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', message).strip()
        if not message:
            return
        with self.lock:
            self.logs.append(dict(time=time.strftime('%H:%M:%S'), channel=name, text=message[-2000:]))

    def start(self, operation, selected, export_options=None):
        if operation not in ('sync', 'organize', 'export', 'snapshot', 'organize_all'):
            raise ValueError('Unknown operation.')
        if not isinstance(selected, list) or not all(isinstance(n, str) for n in selected):
            raise ValueError('Invalid channel selection.')
        names = {r['name'] for r in self.rows}
        if operation not in ('snapshot', 'organize_all') and (not selected or not set(selected) <= names):
            raise ValueError('Select at least one existing channel.')
        options = dce_exports.validate(export_options) if operation == 'export' else None
        with self.lock:
            if self.running:
                raise ValueError('An operation is already running.')
            self.running, self.operation, self.error = True, operation, None
            self.export_options = options
            self.maintenance = None
            self.cancel.clear()
            for row in self.rows:
                row.update(status='queued' if row['name'] in selected else 'idle',
                           percent=None, downloaded=0, detail='Queued' if row['name'] in selected else 'Ready',
                           started=None, elapsed=0)
            threading.Thread(target=self.run, args=(operation, set(selected)), daemon=True).start()

    def export(self, row, token, binary, stage):
        standalone = self.operation == 'export'
        last = None if standalone or self.options['full_history'] else dce.parse_last_after(self.output, row['id'])
        cmd = dce._build_export_cmd(binary, token, row['id'], stage, last) + ['--include-threads', 'None']
        # Supply the token through the environment, not the process list.
        token_index = cmd.index('-t')
        del cmd[token_index:token_index + 2]
        if standalone:
            format_index = cmd.index('-f')
            del cmd[format_index:format_index + 2]
            cmd += dce_exports.arguments(self.export_options)
        cmd += ['--respect-rate-limits', 'true', '--utc', str(self.options['utc']).lower(),
                '--markdown', str(self.options['markdown']).lower()]
        media_dir = stage / 'media' if standalone else self.output / 'media' / row['id']
        if self.options['media']:
            media_dir.mkdir(parents=True, exist_ok=True)
            cmd += ['--media', 'true', '--reuse-media', str(self.options['reuse_media']).lower(),
                    '--media-dir', str(media_dir)]
        media_before = sum(p.stat().st_size for p in media_dir.rglob('*') if p.is_file()) if self.options['media'] else 0
        def downloaded_bytes():
            total = sum(p.stat().st_size for p in stage.rglob('*') if p.is_file())
            if self.options['media'] and not standalone:
                total += max(0, sum(p.stat().st_size for p in media_dir.rglob('*') if p.is_file()) - media_before)
            return total
        self.update(row, status='downloading' , detail=f'Downloading since {last or "the beginning"}', started=time.monotonic())
        self.log(row['name'], row['detail'])
        for attempt in range(self.options['retries'] + 1):
            if self.cancel.is_set():
                return False
            # A failed attempt can leave truncated or differently partitioned files.
            # Never mix those with the successful retry.
            if attempt:
                for leftover in stage.iterdir():
                    if leftover.is_file():
                        leftover.unlink()
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding='utf-8', errors='replace', bufsize=1,
                                    env=dict(os.environ, DISCORD_TOKEN=token))
            def read_output():
                for line in proc.stdout:
                    self.log(row['name'], line, token)
                    clean = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', line)
                    match = re.search(r'(?<!\d)(\d{1,3}(?:[.,]\d+)?)\s*%', clean)
                    if match:
                        self.update(row, percent=min(100, float(match[1].replace(',', '.'))))
            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            try:
                while proc.poll() is None:
                    if self.cancel.wait(.4):
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        break
                    size = downloaded_bytes()
                    self.update(row, downloaded=size)
                proc.wait()
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                reader.join(timeout=5)
                proc.stdout.close()
            if self.cancel.is_set():
                return False
            self.update(row, downloaded=downloaded_bytes())
            if proc.returncode == 0:
                return True
            if attempt < self.options['retries']:
                self.update(row, detail=f"Retrying download ({attempt + 2}/{self.options['retries'] + 1})", percent=None)
                self.log(row['name'], f'Exporter exited with code {proc.returncode}; retrying in {2 ** (attempt + 1)} s.')
                if self.cancel.wait(2 ** (attempt + 1)):
                    return False
        raise RuntimeError(f'Exporter failed (exit code {proc.returncode}). See the activity log for details.')

    def run_channel(self, row, operation, token, binary):
        if self.cancel.is_set():
            self.update(row, status='cancelled', detail='Stopped')
            return
        stage = None
        try:
            incoming = None
            pulled = None
            if operation in ('sync', 'export'):
                parent = dce_storage.report_folder(self.output, dict(self.cfg['channels'][row['name']], name=row['name'], display_name=row['display_name'], category=row.get('category') or self.cfg['channels'][row['name']].get('category'), server=row['server'], id=row['id'])) if operation == 'export' else self.output / '.downloads'
                parent.mkdir(parents=True, exist_ok=True)
                stage = parent / f'.partial-{row["id"]}-{uuid4().hex}'
                stage.mkdir(parents=True)
                # Anchor at start, not finish: a run crossing midnight must overlap.
                pulled = date.today()
                if not self.export(row, token, binary, stage):
                    self.update(row, status='cancelled', detail='Stopped; archive unchanged')
                    shutil.rmtree(stage)
                    return
                if operation == 'export':
                    extension = dce_exports.FORMATS[self.export_options['format']]
                    if not list(stage.glob('*' + extension)):
                        raise ValueError('Exporter produced no output files. See the activity log.')
                    destination = parent / f'{date.today()}-{row["id"]}-{uuid4().hex[:8]}'
                    os.replace(stage, destination)
                    self.update(row, status='done', percent=100, detail=f'Export saved: {destination.relative_to(self.output)}')
                    self.log(row['name'], row['detail'])
                    return
                incoming = sorted(stage.glob('*.json'))
                if not incoming:
                    raise ValueError('Exporter produced no JSON. Archive unchanged.')
            if self.cancel.is_set():
                self.update(row, status='cancelled', detail='Stopped; downloaded data remains in .downloads')
                return
            self.update(row, status='merging', detail='Waiting to merge archive', percent=None)
            with self.merge_lock:
                if self.cancel.is_set():
                    self.update(row, status='cancelled', detail='Stopped; downloaded data remains in .downloads')
                    return
                self.update(row, detail='Validating and merging messages')
                result = consolidate(self.output, row['name'], self.cfg['channels'][row['name']], incoming, pulled, layout=self.options['layout'])
            self.update(row, **result, status='done', detail='Archive saved', percent=100,
                        elapsed=round(time.monotonic() - row['started']) if row['started'] else 0)
            self.log(row['name'], f'Saved {result["messages"]:,} messages · merged {result["duplicates"]:,} duplicates.')
            if stage and not any(stage.iterdir()):
                stage.rmdir()
        except Exception as exc:
            message = str(exc).replace(token, '[redacted token]') if token else str(exc)
            self.update(row, status='error', detail=message)
            self.log(row['name'], message)

    def run(self, operation, selected):
        token = ''
        try:
            with archive_lock(self.output):
                if operation == 'organize_all':
                    self.maintenance = 'Saving a recovery version before organizing…'
                    backup = dce_versions.snapshot(self.output, self.config, self.output.parent / (self.output.name + '-versions'), self.cancel,
                        lambda n,total: setattr(self, 'maintenance', f'Saving recovery version {n}/{total} files'))
                    quarantined = dce_storage.isolate_invalid(self.output, self.cancel,
                        lambda n,total: setattr(self, 'maintenance', f'Validating old exports {n}/{total} files'))
                    for path in quarantined:
                        self.log('Recovery', 'Retained unreadable original: ' + path)
                    plan = self.library()
                    failures = list(plan['skipped'])
                    for index, channel in enumerate(plan['channels']):
                        if self.cancel.is_set():
                            break
                        self.maintenance = f'Organizing {index+1}/{len(plan["channels"])} · {channel["display_name"]}'
                        try:
                            result = consolidate(self.output, channel['name'], channel, layout=self.options['layout'])
                            self.log(channel['name'], f'Organized {result["messages"]:,} messages')
                        except Exception as exc:
                            failures.append(channel['name'])
                            self.log(channel['name'], str(exc))
                    if not self.cancel.is_set():
                        dce_storage.organize_reports(self.output, plan['channels'])
                    with self.lock:
                        self.refresh_rows()
                    self.maintenance = ('Organization stopped' if self.cancel.is_set() else 'Library organized') + f'. Recovery version: {backup.name}'
                    if quarantined:
                        self.maintenance += f'. {len(quarantined)} unreadable originals retained in recovery/unreadable-json.'
                    if failures:
                        self.error = f'{len(failures)} items could not be organized and were retained. See the activity log.'
                    return
                if operation == 'snapshot':
                    self.version_status = 'Preparing snapshot…'
                    target = dce_versions.snapshot(self.output, self.config, self.output.parent / (self.output.name + '-versions'),
                                                  self.cancel, lambda done, total: setattr(self, 'version_status', f'Archiving {done}/{total} files'))
                    self.version_status = f'Saved {target.name}'
                    self.log('Archive', self.version_status)
                    return
                if operation in ('sync', 'export'):
                    try:
                        token = dce.load_token(None)
                        binary = dce.find_dce_binary()
                    except SystemExit:
                        raise RuntimeError('Missing token or DiscordChatExporter.Cli. Check your installation and run dce token set.') from None
                with concurrent.futures.ThreadPoolExecutor(max_workers=self.options['jobs']) as pool:
                    futures = [pool.submit(self.run_channel, row, operation, token, binary if operation in ('sync', 'export') else None)
                               for row in self.rows if row['name'] in selected]
                    for future in concurrent.futures.as_completed(futures):
                        future.result()

        except Exception as exc:
            with self.lock:
                self.error = str(exc).replace(token, '[redacted token]') if token else str(exc)
                if operation == 'snapshot':
                    self.version_status = 'Snapshot stopped' if self.cancel.is_set() else 'Snapshot failed'
                for row in self.rows:
                    if row['status'] == 'queued':
                        row.update(status='error', detail=self.error)
        finally:
            with self.lock:
                self.running = False


def make_handler(app, secret):
    # A running backend must serve the same UI throughout its lifetime, even
    # when a checkout is updated. Restart the app to activate a new build.
    assets = {'/icon.svg': ('icon.svg', 'image/svg+xml'), '/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
    assets = {path: (files('dce_ui').joinpath(name).read_text(encoding='utf-8'), mime)
              for path, (name, mime) in assets.items()}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, payload, content_type='application/json; charset=utf-8'):
            data = payload.encode() if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' https://cdn.discordapp.com; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            return secrets.compare_digest(self.headers.get('X-DCE-Key', ''), secret)

        def do_GET(self):
            path = urlparse(self.path).path
            if path in assets:
                asset, mime = assets[path]
                return self.reply(200, asset, mime)
            if not self.authorized():
                return self.reply(403, {'error': 'Open the app using dce app.'})
            if path == '/api/settings':
                return self.reply(200, app.settings_snapshot())
            if path == '/api/library':
                return self.reply(200, app.library())
            if path == '/api/catalog':
                return self.reply(200, app.catalog_snapshot())
            if path == '/api/state':
                return self.reply(200, app.snapshot())
            self.reply(404, {'error': 'Not found'})

        def do_POST(self):
            if not self.authorized():
                return self.reply(403, {'error': 'Invalid access key.'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536:
                    raise ValueError('Invalid request size.')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('Request body must be an object.')
                if self.path == '/api/settings':
                    return self.reply(200, app.save_settings(body.get('options')))
                elif self.path == '/api/schedule':
                    return self.reply(200, app.save_schedule(body))
                elif self.path == '/api/folder/choose':
                    return self.reply(200, app.choose_folder())
                elif self.path == '/api/storage':
                    app.change_storage(body.get('path'), body.get('copy_existing'))
                elif self.path == '/api/token':
                    return self.reply(200, app.save_token(body.get('token')))
                elif self.path == '/api/discover':
                    app.load_catalog(body.get('guild'), body.get('refresh', False), body.get('threads', 'None'))
                elif self.path == '/api/channels/add':
                    return self.reply(200, app.add_channels(body.get('channels'), body.get('sync', False)))
                elif self.path == '/api/start':
                    app.start(body['operation'], body['channels'], body.get('options'))
                elif self.path == '/api/cancel':
                    app.cancel.set()
                elif self.path == '/api/open':
                    return self.reply(200, app.open_folder(body.get('location', 'archive'), body.get('channel')))
                else:
                    return self.reply(404, {'error': 'Not found'})
                self.reply(200, {'ok': True})
            except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
                self.reply(400, {'error': str(exc)})
    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('channels.yaml'))
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args(argv)
    config = args.config.expanduser().resolve()
    cache = Path.home() / '.cache' / 'dce-sync'
    cache.mkdir(parents=True, exist_ok=True)
    instance = cache / (hashlib.sha256(str(config).encode()).hexdigest()[:20] + '.app')
    with instance.open('a+') as handle:
        os.chmod(instance, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # The existing instance holds the descriptor for its entire lifetime.
            for _ in range(20):
                handle.seek(0)
                url = handle.read().strip()
                if url.startswith('http://127.0.0.1:'):
                    print(f'Discord Archive: {url}', flush=True)
                    if not args.no_browser:
                        webbrowser.open(url)
                    return 0
                time.sleep(.1)
            parser.error('The app is starting. Try opening it again in a moment.')
        handle.seek(0)
        handle.truncate()
        app = Dashboard(config)
        secret = secrets.token_urlsafe(32)
        server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(app, secret))
        url = f'http://127.0.0.1:{server.server_port}/#{secret}'
        handle.write(url)
        handle.flush()
        print(f'Discord Archive: {url}', flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever(poll_interval=.3)
        except KeyboardInterrupt:
            app.cancel.set()
            while app.running:
                time.sleep(.1)
        finally:
            server.server_close()
            handle.seek(0)
            handle.truncate()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
