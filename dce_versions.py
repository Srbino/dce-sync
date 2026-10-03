"""Explicit immutable workspace snapshots, separate from the working archive."""
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
from uuid import uuid4


def snapshot(output, config, destination, cancel, progress):
    """Caller holds archive_lock. Include legacy assets; exclude staging and reports."""
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
    target = destination / f'archive-{stamp}.tar.gz'
    temporary = target.with_suffix('.tmp')
    sources = []
    for path in sorted(output.rglob('*')):
        relative = path.relative_to(output)
        if any(part.startswith('.') for part in relative.parts) or relative.parts[0] == 'reports':
            continue
        if path.is_symlink():
            raise ValueError(f'Snapshot does not follow symbolic links: {relative}')
        if path.is_file():
            sources.append((path, 'exports/' + relative.as_posix()))
    if config.is_file():
        sources.append((config, 'channels.yaml'))
    prefs = config.with_name(config.name + '.dashboard.yaml')
    if prefs.is_file():
        sources.append((prefs, 'dashboard.yaml'))
    manifest = dict(schema=1, created=datetime.now(timezone.utc).isoformat(), files=[])
    try:
        with temporary.open('xb') as raw:
            os.chmod(temporary, 0o600)
            with tarfile.open(fileobj=raw, mode='w:gz') as archive:
                for index, (path, name) in enumerate(sources):
                    if cancel.is_set():
                        raise InterruptedError('Snapshot stopped. No incomplete version was published.')
                    digest = hashlib.sha256()
                    with path.open('rb') as handle:
                        while block := handle.read(1024 * 1024):
                            if cancel.is_set():
                                raise InterruptedError('Snapshot stopped. No incomplete version was published.')
                            digest.update(block)
                    archive.add(path, arcname=name, recursive=False)
                    manifest['files'].append(dict(path=name, bytes=path.stat().st_size, sha256=digest.hexdigest()))
                    progress(index + 1, len(sources))
                data = json.dumps(manifest, indent=2).encode()
                info = tarfile.TarInfo('manifest.json')
                info.size, info.mode = len(data), 0o600
                archive.addfile(info, io.BytesIO(data))
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
