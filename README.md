# dce

A thin wrapper around [DiscordChatExporter.Cli](https://github.com/Tyrrrz/DiscordChatExporter) that adds the few things the underlying CLI doesn't do:

- **Auto-token** — reads the token from the GUI's `Settings.dat`, so you don't have to keep `-t TOKEN` on every command (or stash it in your shell history).
- **Friendly channel names** — a small `channels.yaml` registry maps names you type (`pvm`, `taming`) to channel IDs.
- **Incremental sync** — `dce sync` parses existing export filenames to figure out the latest `--after` date per channel and only pulls new messages.

Everything else is forwarded to `DiscordChatExporter.Cli` unchanged, so the full upstream API is available. No shadow API to maintain.

## Install

```sh
# 1. DiscordChatExporter.Cli (the real tool)
dotnet tool install -g DiscordChatExporter.Cli
# add ~/.dotnet/tools to your PATH if it isn't already

# 2. this wrapper
pip install pyyaml
git clone <this-repo>
ln -s "$PWD/dce-sync/dce" /usr/local/bin/dce   # or add the repo to PATH
```

## Quick start

```sh
cd /path/to/where/you/want/exports
cp /path/to/dce-sync/channels.example.yaml channels.yaml
$EDITOR channels.yaml          # add your channel IDs

dce list                       # see what's registered
dce sync                       # incremental pull
dce sync pvm                   # one channel
dce sync --dry-run             # preview without exporting
```

## How it finds the token

In order, first hit wins:

1. `$DCE_TOKEN` environment variable
2. `--settings PATH` (point to your DCE `Settings.dat`)
3. `~/Library/Application Support/DiscordChatExporter/Settings.dat` (macOS default)
4. `~/.config/DiscordChatExporter/Settings.dat` (Linux default)

If you've opened the DiscordChatExporter GUI once and signed in, the token is already in `Settings.dat` and `dce` will use it without further config.

> **Note** — some macOS installs of DCE keep `Settings.dat` *inside* the `.app` bundle (`DiscordChatExporter.app/Contents/MacOS/Settings.dat`) instead of `~/Library/Application Support`. If that's you, pass `--settings /path/to/that/Settings.dat`.

## How incremental sync works

DiscordChatExporter writes filenames like:

```
Guild - Channel [123456789] (after 2026-05-11).json
```

`dce sync` looks at the output directory, finds the latest `(after YYYY-MM-DD)` marker for each registered channel ID, and reuses that as the new `--after`. New file → no overlap, no manual bookkeeping.

## channels.yaml

```yaml
output_dir: .         # where exports land; relative paths resolve against this file
channels:
  pvm:    { id: "529041672999403554" }
  taming: { id: "515940044414910474" }
```

Get channel IDs with `dce guilds` and `dce channels -g GUILD_ID` (both are passthroughs to DCE.Cli).

## Passthrough

Anything `dce` doesn't recognize as one of its own subcommands is forwarded to `DiscordChatExporter.Cli` with `-t TOKEN` injected:

```sh
dce guilds                                    # list guilds
dce channels -g 290936867199909888            # list channels in a guild
dce export -c 529041672999403554 --after 2026-05-01 -f Json -o ./
dce exportguild -g 290936867199909888 -f Json -o ./
```

If you ever pass `-t` explicitly, the wrapper won't add a second one.

## What it deliberately doesn't do

- Doesn't reimplement DCE.Cli's commands one by one — passthrough handles them.
- Doesn't call the Discord API directly — keeps the on-disk message format identical to what DCE.Cli writes.
- Doesn't write a second copy of your token to disk.

## License

MIT
