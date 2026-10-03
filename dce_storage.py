"""Archive inventory, layout planning and non-destructive workspace relocation."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

import yaml
import dce_sync as dce
import dce_settings


def metadata(path):
    with path.open(encoding='utf-8-sig') as handle:
        prefix = handle.read(131072)
    result = {}
    for field in ('guild', 'channel'):
        match = re.search(r'"' + field + r'"\s*:\s*', prefix)
        if match:
            value, _ = json.JSONDecoder().raw_decode(prefix[match.end():])
            if isinstance(value, dict):
                result[field] = value
    return result


def inventory(output, registry):
    channels, skipped = {}, []
    registered = {str(c['id']): (n, c) for n, c in registry.items()}
    for path in dce.export_files(output):
        try:
            meta = metadata(path)
            channel, guild = meta.get('channel', {}), meta.get('guild', {})
            cid = str(channel.get('id', ''))
            if not cid.isdigit() or f'[{cid}]' not in path.name:
                raise ValueError('Missing or inconsistent channel ID')
            alias, saved = registered.get(cid, (channel.get('name', cid), {}))
            row = channels.setdefault(cid, dict(id=cid, name=alias, display_name=saved.get('display_name') or channel.get('name', alias),
                server=saved.get('server') or guild.get('name', 'Other servers'),
                category=saved.get('category') or channel.get('category') or 'Uncategorized',
                guild_id=saved.get('guild_id') or str(guild.get('id', '')), tracked=cid in registered, files=0, bytes=0))
            row['files'] += 1
            row['bytes'] += path.stat().st_size
        except (ValueError, OSError, TypeError):
            skipped.append(path.name)
    rows = sorted(channels.values(), key=lambda r: (r['server'], r['category'], r['display_name']))
    return dict(channels=rows, files=sum(r['files'] for r in rows), bytes=sum(r['bytes'] for r in rows), skipped=skipped)


def validate_destination(output, value):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError('Choose an absolute folder path.')
    destination = Path(value).expanduser()
    if not destination.is_absolute():
        raise ValueError('Choose an absolute folder path.')
    destination = destination.resolve()
    source = output.resolve()
    if destination == source or destination in source.parents or source in destination.parents:
        raise ValueError('Choose a new folder outside the current archive.')
    if destination.exists() and (not destination.is_dir() or any(p.name != '.dce-sync.lock' for p in destination.iterdir())):
        raise ValueError('Choose an empty folder. Existing files will not be overwritten.')
    return destination


def relocate(output, config, expected_config, destination, copy_existing, cancel, progress):
    """Copy and verify before changing the registry. Original files are retained."""
    destination = validate_destination(output, str(destination))
    original = config.read_text() if config.exists() else ''
    if config.exists() and (yaml.safe_load(original) or {}) != expected_config:
        raise ValueError('Configuration changed outside the app. Reopen the app first.')
    files = [p for p in output.rglob('*') if p.is_file() and p.name != '.dce-sync.lock'] if copy_existing else []
    if any(p.is_symlink() for p in output.rglob('*')):
        raise ValueError('Relocation does not follow symbolic links.')
    destination.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(destination).free < sum(p.stat().st_size for p in files):
        raise ValueError('Not enough free space at the destination.')
    from dce_archive import archive_lock
    target_lock = archive_lock(destination)
    target_lock.__enter__()
    created = []
    try:
        for index, source in enumerate(files):
            if cancel.is_set():
                raise InterruptedError('Folder change stopped; original archive remains active.')
            target = destination / source.relative_to(output)
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with source.open('rb') as reader, target.open('xb') as writer:
                created.append(target)
                while chunk := reader.read(1024 * 1024):
                    if cancel.is_set():
                        raise InterruptedError('Folder change stopped; original archive remains active.')
                    writer.write(chunk); digest.update(chunk)
                writer.flush(); os.fsync(writer.fileno())
            verify = hashlib.sha256()
            with target.open('rb') as reader:
                while chunk := reader.read(1024 * 1024): verify.update(chunk)
            if verify.digest() != digest.digest():
                raise ValueError('Copy verification failed; original archive remains active.')
            progress(index + 1, len(files))
        if cancel.is_set():
            raise InterruptedError('Folder change stopped; original archive remains active.')
        if (config.read_text() if config.exists() else '') != original:
            raise ValueError('Configuration changed while copying; original archive remains active.')
        cfg = dict(expected_config, output_dir=str(destination))
        scalar = json.dumps(str(destination), ensure_ascii=False)
        updated, count = re.subn(r'^output_dir:.*$', lambda _: 'output_dir: ' + scalar, original, count=1, flags=re.M)
        if not count: updated = 'output_dir: ' + scalar + '\n' + original
        if not original: updated = yaml.safe_dump(cfg, sort_keys=False)
        if yaml.safe_load(updated) != cfg:
            raise ValueError('Could not preserve the configuration. No settings were changed.')
        dce_settings.atomic_write(config, updated)
        return cfg
    except Exception:
        for target in created: target.unlink(missing_ok=True)
        for folder in sorted((p for p in destination.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try: folder.rmdir()
            except OSError: pass
        raise
    finally:
        target_lock.__exit__(None, None, None)


def report_folder(output, channel):
    from dce_archive import safe_name
    return output / 'reports' / safe_name(channel.get('server') or 'Other servers') / safe_name(channel.get('category') or 'Uncategorized') / f'{safe_name(channel.get("display_name") or channel.get("name") or channel["id"])} [{channel["id"]}]'


def organize_reports(output, channels):
    """Move self-contained report directories; retain HTML with local dependencies."""
    from html.parser import HTMLParser
    from urllib.parse import urlparse
    by_id = {r['id']:r for r in channels}
    moved = 0
    for source in list((output / 'reports').glob('*')):
        match = re.fullmatch(r'\d{4}-\d\d-\d\d-(\d+)-[a-f0-9]+', source.name)
        if source.is_dir() and match and match[1] in by_id:
            target = report_folder(output, by_id[match[1]]) / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists(): source.rename(target); moved += 1
    class Dependencies(HTMLParser):
        local = False
        def handle_starttag(self, tag, attrs):
            for key,value in attrs:
                if key in ('src','href','poster') and value and not value.startswith(('#','//')) and not urlparse(value).scheme:
                    self.local = True
    for source in output.glob('*.html'):
        match = re.search(r'\[(\d+)\]', source.name)
        if not match or match[1] not in by_id: continue
        parser = Dependencies()
        with source.open(encoding='utf-8-sig') as handle:
            for chunk in iter(lambda:handle.read(1024*1024), ''): parser.feed(chunk)
        if parser.local: continue
        target = report_folder(output, by_id[match[1]]) / 'legacy' / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists(): source.rename(target); moved += 1
    return moved


def isolate_invalid(output, cancel, progress):
    """After a recovery snapshot, retain unreadable exports outside the merge set."""
    from uuid import uuid4
    quarantined = []
    candidates = dce.export_files(output)
    for index, source in enumerate(candidates):
        if cancel.is_set(): break
        progress(index+1,len(candidates))
        try:
            # Only handle recognizable exporter documents, not arbitrary user JSON.
            head = metadata(source)
            cid = str(head.get('channel', {}).get('id', ''))
            if not cid.isdigit() or f'[{cid}]' not in source.name: continue
        except (OSError, ValueError, TypeError):
            continue
        try:
            with source.open(encoding='utf-8-sig') as handle: data = json.load(handle)
            if not isinstance(data, dict) or not isinstance(data.get('messages'), list) or any(not isinstance(m,dict) or not m.get('id') for m in data['messages']):
                raise ValueError('Invalid message structure')
            del data
        except (ValueError, TypeError) as exc:
            target = output/'recovery'/'unreadable-json'/source.relative_to(output)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists(): target = target.with_name(uuid4().hex[:8]+'-'+target.name)
            source.rename(target)
            note = target.with_suffix(target.suffix+'.txt')
            note.write_text('Retained original export. Not included in the merged archive.\nValidation error: '+str(exc)+'\n',encoding='utf-8')
            quarantined.append(str(target.relative_to(output)))
    return quarantined
