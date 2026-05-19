# dce

A thin wrapper around [DiscordChatExporter.Cli](https://github.com/Tyrrrz/DiscordChatExporter) that adds the few things the underlying CLI doesn't do:

- **Auto-token** — reads the token from the GUI's `Settings.dat`, so you don't have to keep `-t TOKEN` on every command (or stash it in your shell history).
- **Friendly channel names** — a small `channels.yaml` registry maps names you type (`pvm`, `taming`) to channel IDs.
- **Incremental sync** — `dce sync` parses existing export filenames to figure out the latest `--after` date per channel and only pulls new messages.

Everything else is forwarded to `DiscordChatExporter.Cli` unchanged, so the full upstream API is available. No shadow API to maintain.

## Install

```sh
# 1. DiscordChatExporter.Cli (the real tool) — get the latest release
# from https://github.com/Tyrrrz/DiscordChatExporter/releases and unzip
# the appropriate self-contained build for your platform somewhere on PATH
# (e.g. ~/.local/share/dce-cli/, with a thin shim at ~/.local/bin/discordchatexporter).

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

dce token set YOUR_DISCORD_TOKEN   # see "Getting your Discord token" below

dce list                       # see what's registered
dce sync                       # incremental pull
dce sync pvm                   # one channel
dce sync --dry-run             # preview without exporting
```

## How it finds the token

In order, first hit wins:

1. `$DCE_TOKEN` environment variable
2. `~/.config/dce-sync/token` (set once via `dce token set <TOKEN>`, stored with mode 0600)
3. `./.dce_token` in the current directory (project-local override)
4. DCE GUI `Settings.dat`, **only if it isn't encrypted** — current DCE versions store the token as `enc:...` and we can't decrypt that without the GUI's platform-specific key derivation, so this path is essentially a legacy fallback.

### Getting your Discord token

`dce` does not extract tokens from the Discord client or browser. Use the same approach DCE.Cli documents (`discordchatexporter guide`):

1. Open Discord in a browser, log in, press <kbd>F12</kbd> to open Dev Tools.
2. In the **Network** tab, filter for `/api`.
3. Click any request, look in the request headers for `Authorization: ...` — that string is your user token.
4. `dce token set <THAT_STRING>` — wraps with the right env semantics; nothing else has to know about it.

> **Account risk** — user tokens are not officially supported. Discord may rate-limit or suspend accounts that automate them. Use on accounts you own, accept the risk. `dce` defaults to DCE.Cli's polite rate-limit preset.

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
