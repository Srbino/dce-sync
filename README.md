# dce

[![test](https://github.com/Srbino/dce-sync/actions/workflows/test.yml/badge.svg)](https://github.com/Srbino/dce-sync/actions/workflows/test.yml)

A thin wrapper around [DiscordChatExporter.Cli](https://github.com/Tyrrrz/DiscordChatExporter) that adds the bits the underlying CLI doesn't ship:

- **Friendly channel names** via a small `channels.yaml` registry — type `dce sync pvm` instead of `discordchatexporter export -c 529041672999403554 …`.
- **Persistent token storage** with sensible discovery order so `-t TOKEN` never has to live in shell history.
- **Smart incremental sync** — parses existing export filenames so `dce sync` only pulls new messages per channel.
- **Parallel multi-channel sync** with optional periodic size/delta snapshots and retry with exponential backoff.
- **Local archive tools** — `verify`, `stats`, `merge`, `snapshot`, `search`, `export-csv`, `status`.
- **Machine-readable output** — `--json` on every informational command (list, stats, search, verify, status) for jq / dashboards / cron pipelines.

Anything `dce` doesn't recognize is forwarded to `DiscordChatExporter.Cli` unchanged (with `-t TOKEN` auto-injected), so the full upstream surface area stays available. No shadow API to maintain.

## Install

```sh
# 1. DiscordChatExporter.Cli (the actual tool). Grab the self-contained
#    build for your platform from the upstream releases page and put a
#    shim on PATH. For example on macOS arm64:
#
#      mkdir -p ~/.local/share/dce-cli ~/.local/bin
#      curl -L -o /tmp/dce.zip \
#        https://github.com/Tyrrrz/DiscordChatExporter/releases/latest/download/DiscordChatExporter.Cli.osx-arm64.zip
#      unzip /tmp/dce.zip -d ~/.local/share/dce-cli
#      chmod +x ~/.local/share/dce-cli/DiscordChatExporter.Cli
#      cat > ~/.local/bin/discordchatexporter <<'SHIM'
#      #!/bin/sh
#      exec "$HOME/.local/share/dce-cli/DiscordChatExporter.Cli" "$@"
#      SHIM
#      chmod +x ~/.local/bin/discordchatexporter
#
# 2. dce itself.
git clone <this-repo>
cd dce-sync
pip install .              # installs the `dce` console script
# or:  pip install -e .    # editable install for hacking
# or:  chmod +x dce && ln -s "$PWD/dce" ~/.local/bin/dce   # no install at all
```

Optional: tab completion.

```sh
dce completion zsh  > ~/.zfunc/_dce       # then in .zshrc:
                                          #   fpath=(~/.zfunc $fpath)
                                          #   autoload -Uz compinit && compinit
dce completion bash > ~/.bash_completion.d/dce
```

## Quick start

```sh
cd /path/to/where/you/want/exports
cp /path/to/dce-sync/channels.example.yaml channels.yaml
$EDITOR channels.yaml          # add your channel IDs (or use `dce discover`)

dce token set YOUR_DISCORD_TOKEN   # see "Getting your token" below
dce list                           # show registry + last-export dates
dce sync                           # incremental pull (all channels)
```

## Command reference

| Command | Purpose |
| --- | --- |
| `dce list [--json]` | Show registered channels and the latest `(after X)` date in `output_dir` for each. |
| `dce sync [name…]` | Incremental sync. Flags below. |
| `dce status` | One-shot health snapshot (token age, channels, archive size, DCE.Cli version). `[--json] [--verify] [--check-updates]`. |
| `dce add NAME CHANNEL_ID` | Register a channel manually. |
| `dce discover --guild GID` | List a server's channels (and optionally append new ones to `channels.yaml`). |
| `dce token set/show/path/rm` | Manage the persisted Discord token. |
| `dce token age [--max-days N]` | Rotation reminder; exits 1 when the saved token is older than the cap. |
| `dce stats [--fast] [--json]` | Per-channel totals: files, msgs, size, date range. |
| `dce verify [--quick] [--json]` | Sanity-check every JSON in `output_dir` (OK / TRUNCATED / CORRUPT / …). |
| `dce merge [name…]` | Consolidate per-channel `(after X)` files into a single deduped archive. |
| `dce search PATTERN [name…] [--json]` | Grep messages across the archive. `--json` emits JSONL. |
| `dce export-csv [name…]` | Dump messages to CSV (one row per message). |
| `dce snapshot` | Bundle `channels.yaml` + every export into a single `.tar.gz` with a manifest. |
| `dce upgrade-check` | Compare installed `DCE.Cli` against the latest GitHub release. |
| `dce completion zsh\|bash` | Print a completion script to stdout. |
| `dce <anything-else>` | Passthrough to `DiscordChatExporter.Cli` with `-t TOKEN` injected. |

### `dce sync` flags

```text
dce sync [name …]
  --dry-run                 print the planned command(s) without exporting
  -j, --jobs N              run up to N channel exports in parallel
  --since 7d|3w|2m|1y       override file-based last_after with NOW - X
  --until YYYY-MM-DD        upper bound; passes --before to DCE.Cli
  --watch [SECONDS]         in parallel mode, print a size/delta snapshot
                            every N seconds (default 10s)
  -q, --quiet               suppress chatter; only failures + final summary
                            (also via DCE_QUIET=1 in the environment)
  --retries N               retry transient subprocess failures with
                            exponential backoff (default 0)
```

### Examples

```sh
# parallel sync with periodic progress, fall back to retries on flaky network
dce sync -j 4 --watch 10 --retries 3

# force-refresh the last week across every registered channel
dce sync --since 7d

# historic backfill of a fixed window
dce sync --since 90d --until 2026-01-01

# daily cron health check; nonzero exit triggers your mailer
dce status --check-updates --verify || mail -s "dce archive" me@…

# stale-channel detector
dce list --json | jq -r '.channels[] | select(.last_after == null) | .name'

# silent cron-style sync (DCE_QUIET also works)
dce sync --quiet --retries 2

# grep the archive
dce search frostbane                          # substring, all channels
dce search 'wild.*spawn' --regex pvm taming   # regex on a subset
dce search news --author witcher --from 2026-04-01

# dump pvm last week to CSV
dce export-csv pvm --from 2026-05-13 -o /tmp/pvm.csv

# consolidate split exports and reclaim disk
dce merge --dry-run            # preview dedup numbers
dce merge                      # do it

# back up the whole archive
dce snapshot -o ~/backups/discord-$(date +%F).tar.gz
```

## How it finds the token

In order, first hit wins:

1. `$DCE_TOKEN` environment variable
2. `~/.config/dce-sync/token` (set once via `dce token set <TOKEN>`, stored with mode 0600)
3. `./.dce_token` in the current directory (project-local override)
4. DCE GUI `Settings.dat`, **only if it isn't encrypted** — modern DCE GUI versions write the token as `enc:…` and we can't decrypt that without the GUI's platform-specific key derivation; this path stays as a legacy fallback.

### Getting your Discord token

`dce` does not extract tokens from the Discord client or browser. Use the standard approach (also explained by `discordchatexporter guide`):

1. Open Discord in a browser, log in, press <kbd>F12</kbd> to open DevTools.
2. In the **Network** tab, filter for `/api`.
3. Click any request and copy the `Authorization` request header.
4. `dce token set <THAT_STRING>` — it's written to `~/.config/dce-sync/token` with mode 0600.

> **Account risk** — user tokens are not officially supported. Discord may rate-limit or suspend accounts that automate them. Use on accounts you own and accept the risk. `dce` defers to `DCE.Cli`'s polite rate-limit preset.

## How incremental sync works

`DiscordChatExporter.Cli` writes filenames like:

```
Guild - Channel [123456789] (after 2026-05-11).json
```

`dce sync` lists `output_dir`, finds the latest `(after YYYY-MM-DD)` marker for each registered channel ID, and reuses that as the new `--after`. The result is a new file each run with no overlap and no manual bookkeeping — and when the per-channel file list gets noisy you can collapse it back to one archive with `dce merge`.

After each successful export, `dce sync` post-renames the file to add a `(pulled YYYY-MM-DD)` suffix:

```
Guild - Channel [123456789] (after 2026-05-11) (pulled 2026-05-20).json
```

That way each calendar day produces a separate snapshot (intra-day re-syncs overwrite the day's file), so the archive grows linearly with activity rather than with sync count, and you can answer "what did this channel look like on date X" by opening the file with the matching pulled date. `parse_last_after` reads both stamped and unstamped filenames.

`--since 7d|3w|2m|1y` bypasses this and forces every channel to start from NOW minus the given window. Overlapping data gets deduped by `dce merge` if you care.

## `channels.yaml`

```yaml
output_dir: .         # where exports land; relative paths resolve against this file
channels:
  pvm:    { id: "529041672999403554" }
  taming: { id: "515940044414910474" }
```

Discovering channel IDs:

```sh
dce guilds                                   # passthrough to DCE.Cli
dce channels -g 290936867199909888           # passthrough; raw list
dce discover --guild 290936867199909888      # parsed table, [new]/[existing] status
dce discover --guild 290936867199909888 --filter pvm --write   # append new only
```

## Passthrough

Anything `dce` doesn't recognize as one of its own subcommands gets forwarded to `DiscordChatExporter.Cli` with `-t TOKEN` injected after the subcommand:

```sh
dce guilds
dce channels -g 290936867199909888
dce export -c 529041672999403554 --after 2026-05-01 -f Json -o ./
dce exportguild -g 290936867199909888 -f Json -o ./
```

If you pass `-t` explicitly, the wrapper won't add a second one.

## Desktop launcher (macOS)

For the days you'd rather click than type. `./desktop/install-app.sh` puts an
`Outlands Discord.app` on the Desktop; double-clicking it opens Terminal, prints
a read-only overview (archive path and size, token age, DCE.Cli version, and a
per-server tree of what is synced through when and what the run will fetch),
waits for a yes, then syncs the priority Discord first and the backfill after —
so an aborted run still leaves the channels you care about current. Closes with
a list of exactly which files are new.

The overview is Czech; the bundle is a shim pointing back at the checkout, so
pulling the repo changes what the icon does. See [`desktop/README.md`](desktop/README.md).

## What it deliberately doesn't do

- Doesn't reimplement `DCE.Cli`'s commands one by one — passthrough handles them.
- Doesn't call the Discord API directly — keeps the on-disk message format identical to what `DCE.Cli` writes, which means `search` / `merge` / `export-csv` all stay format-compatible with anything else that parses DCE exports.
- Doesn't write a second copy of your token to disk.
- Doesn't try to be a search engine — `dce search` is a linear `json.load` across files. For frequent queries against a large archive, feed it into a SQLite+FTS5 indexer; that's a separate tool, not this one.

## License

MIT
