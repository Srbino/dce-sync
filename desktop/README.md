# Desktop launcher (macOS)

A double-clickable icon that opens the local English dashboard in your browser.

```sh
./desktop/install-app.sh              # creates/updates "Discord Archive.app"
./desktop/install-app.sh --workspace /path/to/registry --dest ~/Applications
```

The app uses the checkout's `.venv/bin/python` when available, with `python3` as a
fallback. Install dependencies with `pip install -e .` in that environment first.
The bundle points back at this checkout; no frontend build or npm install is needed.

A double-click opens the overview without starting any downloads. Select channels,
then use **Sync selected** to download and automatically consolidate their JSON,
or **Organize archive** for offline consolidation. The dashboard has live exporter
output, actual byte counts, channel states, search/filtering, and cancellation.
It reuses the existing server when opened again for the same registry.

Logs for launcher/startup errors are at `~/Library/Logs/dce-sync/app.log`.
The dashboard binds only to localhost. Closing its browser tab keeps the local
server alive; reopening the icon reconnects to the same run. For foreground use:

```sh
.venv/bin/python dce --config /path/to/channels.yaml app
# Ctrl+C stops the server and cancels an active download safely.
```

The original terminal workflow remains available as `desktop/export.command
--workspace /path/to/registry`. Its `--debug`, `--yes`, and `--jobs` options remain
supported. `plan_cz.py` remains the read-only terminal overview.

## Any workspace, any server

The launcher uses a registry in the current directory or checkout, or the explicit
`--workspace` path. It has no server-specific defaults. The dashboard orders servers
alphabetically unless `priority_server: Server name` is set in `channels.yaml`.

The old `plan_cz.py` and `export.command` scripts remain optional legacy Czech
terminal utilities. They are not used by the desktop dashboard.

## Regenerating the icon

`playwright` is not a dependency of this repo — point the script at a project
that has it:

```sh
DCE_PLAYWRIGHT_FROM=/path/to/playwright-project node desktop/make-icon.mjs
```

The chat/archive glyph is original SVG artwork included under the project MIT license.
Set `DCE_CHROMIUM_PATH` when using an existing Chromium or Chrome installation.
