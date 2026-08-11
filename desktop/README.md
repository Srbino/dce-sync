# Desktop launcher (macOS)

A double-clickable icon that shows you what is about to happen, then runs the
sync. Built for the Outlands archive, but nothing here is Outlands-specific
beyond the default priority server.

```sh
./desktop/install-app.sh              # creates "Outlands Discord.app" on the Desktop
```

The bundle is a shim — it opens Terminal and runs `desktop/export.command` out
of this checkout. Pulling the repo changes what the icon does; no reinstall.

## What a double-click does

1. **Pre-flight overview** (`plan_cz.py`, Czech) — where the archive lives, how
   big it is, token age, DCE.Cli version, and a per-server tree of every channel
   with the date it is synced to and what the run will fetch. Read-only.
2. **Confirm** — Enter to go, `q` to bail. Nothing has touched the network yet.
3. **Sync, priority server first** — channels are pulled server by server in the
   order the tree printed. If the run dies halfway or the token has expired, the
   Discord you actually care about is already current.
4. **Closing summary** — elapsed time, which files are new, how much they weigh,
   and an offer to open the folder in Finder.

Hold <kbd>⌥</kbd> while launching for a verbose run (resolved paths, `PATH`,
`set -x`).

## Pieces

| File | Role |
| --- | --- |
| `plan_cz.py` | The overview. `--emit-plan` prints the same grouping machine-readably; `--check-updates` asks GitHub about a newer DCE.Cli (4s budget, offline-safe). |
| `export.command` | The runner: overview → confirm → grouped sync → summary. Usable straight from a shell. |
| `install-app.sh` | Builds the `.app` bundle. `--workspace`, `--dest`, `--name`. |
| `icon.svg` | Icon artwork, in the Outlands Vendor/Wardrobe visual language. |
| `make-icon.mjs` | Rasterises the SVG to `icon.icns`. Only needed if the artwork changes. |

`plan_cz.py` imports dce_sync's own `parse_last_after`, so the dates it prints
are the dates `dce sync` will act on — the overview cannot drift from reality.

## Priority server

`DEFAULT_PRIORITY` in `plan_cz.py` names the Discord that leads the tree and the
sync queue (`Outlands Community`). Override per-run with `--priority`.

Channels are assigned to a server by reading the `<Server> - <Category> - …`
prefix off their existing exports. A channel with nothing downloaded yet falls
back to the `oc-` naming convention, or to an explicit key in `channels.yaml`:

```yaml
channels:
  oc-harvesting:
    id: '1520906212319695108'
    server: Outlands Community
```

## Regenerating the icon

`playwright` is not a dependency of this repo — point the script at a project
that has it:

```sh
DCE_PLAYWRIGHT_FROM=../uo-outlands-vendor-investment node desktop/make-icon.mjs
```

The glyph is game-icons' `chat-bubble` (CC BY 3.0, Lorc — game-icons.net) with a
download arrow masked out of its disc.
