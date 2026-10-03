"""Read Discord server/channel listings through the installed exporter."""
from __future__ import annotations

import os
import re
import subprocess

import dce_sync as dce


_ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
_ROW = re.compile(r'^\s*(\d{1,20})\s*[|│]\s*(.+?)\s*$')


def parse_listing(output: str) -> list[dict]:
    """Read ID | name rows, retaining parent context for indented thread rows."""
    result, seen, parent = [], set(), None
    for line in _ANSI.sub('', output).splitlines():
        thread = re.match(r'^\s*\*\s*(\d{1,20})\s*[|│]\s*(.*?)\s*[|│]\s*(Active|Archived)\s*$', line)
        match = thread or _ROW.match(line)
        if match and match[1] not in seen:
            seen.add(match[1])
            row = {'id': match[1], 'name': match[2]}
            if thread:
                row.update(parent_id=parent['id'] if parent else None,
                           parent_name=parent['name'] if parent else 'Threads',
                           kind='thread', archived_thread=match[3] == 'Archived')
            else:
                parent = row
            result.append(row)
    if output.strip() and not result:
        raise ValueError('Could not parse the Discord listing. Check your DiscordChatExporter.Cli version.')
    return result


def discover(guild: str | None = None, threads: str = 'None') -> list[dict]:
    if threads not in ('None', 'Active', 'All'):
        raise ValueError('Invalid thread mode.')
    try:
        token = dce.load_token(None)
        binary = dce.find_dce_binary()
    except SystemExit:
        raise ValueError('Missing saved Discord token or DiscordChatExporter.Cli.') from None
    command = [binary, 'guilds'] if guild is None else [
        binary, 'channels', '-g', guild, '--include-vc', 'false', '--include-threads', threads]
    # The token stays in the subprocess environment, never in the browser or logs.
    env = dict(os.environ, DISCORD_TOKEN=token, NO_COLOR='1')
    try:
        response = subprocess.run(command, env=env, capture_output=True, text=True,
                                  encoding='utf-8', errors='replace', timeout=180 if threads == 'All' else 75)
    except subprocess.TimeoutExpired:
        raise ValueError('Discord took too long to respond. Try loading again.') from None
    except OSError:
        raise ValueError('Could not start DiscordChatExporter.Cli.') from None
    if response.returncode:
        # Do not expose raw CLI errors, which may echo authentication arguments.
        detail = (response.stderr + response.stdout).lower()
        if '401' in detail or 'unauthorized' in detail or 'token' in detail:
            raise ValueError('Discord rejected the login. Update your saved token using dce token set.')
        if '403' in detail or 'forbidden' in detail:
            raise ValueError('You do not have access to this server or its channels.')
        raise ValueError('Could not load the Discord listing. Check your connection and try again.')
    rows = parse_listing(response.stdout)
    if guild is None:
        icons = guild_icons(token)
        return [dict(r, icon_url=icons.get(r['id'])) for r in rows if r['id'] != '0']
    for row in rows:
        category, _, name = row['name'].rpartition(' / ')
        row.update(category=row.get('parent_name') or category or 'Uncategorized', name=name)
    return rows


def guild_icons(token: str) -> dict[str, str]:
    """Optional display metadata. A failed icon lookup never blocks discovery."""
    import json
    import urllib.error
    import urllib.request
    icons = {}
    authorization = token
    after = '0'
    try:
        for _ in range(10):
            url = f'https://discord.com/api/v10/users/@me/guilds?limit=200&after={after}'
            request = urllib.request.Request(url, headers={'Authorization': authorization,
                                                          'User-Agent': 'DiscordArchive (https://github.com/Srbino/dce-sync, 0.2)'})
            try:
                response = urllib.request.urlopen(request, timeout=8)
            except urllib.error.HTTPError as exc:
                if exc.code != 401 or authorization.startswith('Bot '):
                    return icons
                authorization = 'Bot ' + token
                request.add_header('Authorization', authorization)
                response = urllib.request.urlopen(request, timeout=8)
            with response:
                guilds = json.load(response)
            if not isinstance(guilds, list):
                return icons
            for guild in guilds:
                cid, icon = str(guild.get('id', '')), guild.get('icon')
                if cid.isdigit() and isinstance(icon, str) and re.fullmatch(r'(?:a_)?[a-fA-F0-9]{32}', icon):
                    icons[cid] = f'https://cdn.discordapp.com/icons/{cid}/{icon}.png?size=64'
            if len(guilds) < 200:
                break
            after = str(max(int(g['id']) for g in guilds))
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return icons


def archived_icons(output):
    """Read only the small metadata prefix, never a whole message archive."""
    import json
    icons = {}
    for path in dce.export_files(output):
        try:
            with path.open(encoding='utf-8-sig') as handle:
                prefix = handle.read(65536)
            match = re.search(r'"guild"\s*:\s*', prefix)
            if not match:
                continue
            guild, _ = json.JSONDecoder().raw_decode(prefix[match.end():])
            cid, url = str(guild.get('id', '')), guild.get('iconUrl')
            if isinstance(url, str):
                icon = re.fullmatch(r'https://cdn\.discordapp\.com/icons/(\d+)/((?:a_)?[a-fA-F0-9]{32})\.(?:png|webp|gif)(?:\?.*)?', url)
                if icon and icon[1] == cid:
                    icons[cid] = f'https://cdn.discordapp.com/icons/{cid}/{icon[2]}.png?size=64'
        except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
            continue
    return icons
