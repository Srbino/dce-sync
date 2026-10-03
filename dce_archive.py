"""Transactional, per-channel archive consolidation for the local dashboard."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import dce_sync as dce


@contextlib.contextmanager
def archive_lock(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    with (output / '.dce-sync.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another sync is using this archive.') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def safe_name(value: str) -> str:
    return re.sub(r'[^\w .-]', '_', value).strip(' .')[:100] or 'Uncategorized'


def server_name(output: Path, name: str, channel: dict) -> str:
    if channel.get('server'):
        return str(channel['server'])
    for path in dce._files_for_channel(output, str(channel['id'])):
        if path.parent.parent.parent.name == 'archive':
            return path.parent.parent.name
        if ' - ' in path.name:
            return path.name.split(' - ', 1)[0]
    return 'Other servers'


def _rebase(value, source: Path, destination: Path):
    """Keep existing local media links valid after moving the JSON."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {'url', 'proxyUrl', 'iconUrl', 'avatarUrl'} and isinstance(item, str):
                if item and not urlparse(item).scheme and not item.startswith('//'):
                    value[key] = os.path.relpath((source / item).resolve(), destination)
            else:
                _rebase(item, source, destination)
    elif isinstance(value, list):
        for item in value:
            _rebase(item, source, destination)


def consolidate(output: Path, name: str, channel: dict,
                incoming: list[Path] | None = None, pulled: date | None = None) -> dict:
    """Validate every input, commit atomically, then remove redundant JSON only.

    A failed parse never removes a source. Incoming data wins duplicate IDs.
    The resume marker lives in the filename, keeping the CLI compatible.
    """
    cid = str(channel['id'])
    if not cid.isdigit():
        raise ValueError('Channel IDs must contain only digits.')
    existing = dce._files_for_channel(output, cid)
    files = sorted(existing, key=lambda p: (p.stat().st_mtime_ns, str(p)))
    files += [p for p in (incoming or []) if p not in files]
    if not files:
        return {'messages': 0, 'duplicates': 0, 'files': 0, 'bytes': 0, 'path': None}
    last = dce.parse_last_after(output, cid)
    stamp = max(filter(None, (last, pulled)), default=None)
    folder = output / 'archive' / safe_name(server_name(output, name, channel)) / f'{safe_name(name)} [{cid}]'
    suffix = f' (pulled {stamp.isoformat()})' if stamp else ''
    target = folder / f'messages [{cid}]{suffix}.json'
    merged, meta, total = {}, {}, 0
    for source in files:
        with source.open(encoding='utf-8-sig') as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or not isinstance(data.get('messages'), list):
            raise ValueError(f'{source.name}: invalid export format')
        if str(data.get('channel', {}).get('id', '')) != cid:
            raise ValueError(f'{source.name}: channel ID does not match')
        _rebase(data, source.parent, folder)
        for message in data['messages']:
            if not isinstance(message, dict) or not message.get('id'):
                raise ValueError(f'{source.name}: message is missing an ID')
            merged[str(message['id'])] = message
        total += len(data['messages'])
        meta.update({key: value for key, value in data.items() if key != 'messages'})
    meta['messages'] = sorted(merged.values(), key=lambda m: (m.get('timestamp', ''), str(m['id'])))
    meta['messageCount'] = len(merged)
    # A merged archive is no longer limited to the last incremental window.
    meta['dateRange'] = {'after': None, 'before': None}
    folder.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=folder, suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(meta, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    for source in files:
        if source.resolve() != target.resolve():
            source.unlink()
    return {'messages': len(merged), 'duplicates': total - len(merged), 'files': 1,
            'bytes': target.stat().st_size, 'path': str(target.relative_to(output)),
            'last': stamp.isoformat() if stamp else None}
