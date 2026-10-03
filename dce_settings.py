"""Validated, per-workspace dashboard preferences. Credentials are stored separately."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

DEFAULTS = dict(jobs=3, retries=2, media=False, reuse_media=True, utc=False,
                markdown=True, full_history=False, threads='None')


def validate(values):
    if not isinstance(values, dict) or set(values) - DEFAULTS.keys():
        raise ValueError('Unknown settings.')
    result = dict(DEFAULTS, **values)
    for name, low, high in [('jobs', 1, 6), ('retries', 0, 5)]:
        if type(result[name]) is not int or not low <= result[name] <= high:
            raise ValueError(f'{name} must be an integer from {low} to {high}.')
    for name in ['media', 'reuse_media', 'utc', 'markdown', 'full_history']:
        if type(result[name]) is not bool:
            raise ValueError(f'{name} must be true or false.')
    if result['threads'] not in ('None', 'Active', 'All'):
        raise ValueError('Invalid thread mode.')
    return result


def settings_path(config: Path):
    return config.with_name(config.name + '.dashboard.yaml')


def load(config: Path):
    path = settings_path(config)
    return validate(yaml.safe_load(path.read_text()) or {}) if path.exists() else dict(DEFAULTS)


def atomic_write(path: Path, text: str, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def save(config: Path, values):
    result = validate(values)
    atomic_write(settings_path(config), yaml.safe_dump(result, sort_keys=False))
    return result
