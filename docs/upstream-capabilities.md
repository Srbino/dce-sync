# DiscordChatExporter capability review

Reviewed on 2026-10-03 against **release 2.48**, commit
[`905489b`](https://github.com/Tyrrrz/DiscordChatExporter/tree/2.48).
The downloaded macOS arm64 binary reports `v2.48.0`. A live empty-window export
completed successfully, produced valid JSON with the requested channel ID and
zero messages, and included server-icon metadata. Its command help was
checked directly, alongside the source at the release tag. The development
branch is not the compatibility baseline. The existing local installation was
2.47.3; that version's help and relevant source were also compared.

## What the engine already provides

| Capability | Upstream interface | Implication for this wrapper |
| --- | --- | --- |
| Discover accessible servers | `guilds` | Use the saved account, without asking users to paste IDs. |
| Discover channels and categories | `channels -g ID` | Parse `ID | hierarchical name`; no JSON listing mode is exposed. |
| Discover threads | `channels --include-threads None/Active/All` | Preserve indented thread IDs and their parent channel. |
| Direct messages and group DMs | `dm`, `exportdm` | Available in the picker under Direct messages; chosen conversations use the same per-channel archive pipeline. |
| One or multiple channels | `export -c ID...` | The dashboard uses one channel per subprocess for reliable results. |
| Expand a category | Pass its ID to `export -c` | Category grouping in a UI need not become a new message-fetching implementation. |
| Entire server or account | `exportguild`, `exportall` | Powerful batch tools, but see partial-failure semantics below. |
| Formats | `Json`, `HtmlDark`, `HtmlLight`, `Csv`, `PlainText` | Canonical merged archive stays JSON. Export selected supports all five formats, separate from the archive. |
| Time/message boundaries | `--after`, `--before` | Accept dates, timestamps, or message IDs; the engine does not maintain a persistent sync checkpoint. |
| Threads and forum posts | `--include-threads` | Forums are containers. Their individual threads are exportable channels. |
| Attachments and assets | `--media`, `--reuse-media`, `--media-dir` | Settings exposes media download and reuse with stable per-channel folders. |
| Hierarchical output paths | `%G`, `%T`, `%C`, `%g`, `%c`, etc. | Native filename templates already exist; merging and safe commits remain wrapper responsibilities. |
| Partitioning | `--partition 10000` or `--partition 20mb` | Useful for standalone exports; incompatible with a literal one-file archive unless merged afterward. |
| Filtering | `--filter` | Supports authors, mentions, content types, text, AND/OR/negation and groups. |
| Parallel exports | `--parallel` | No need to invent a new Discord download engine. |
| Formatting | `--markdown`, `--locale`, `--utc`, `--reverse` | Export presentation settings, separate from archive integrity. |
| Discord data package | `exportall --data-package ZIP` | Selects channels referenced by the data package; it is not an offline import of its messages. |

Sources: [CLI documentation](https://github.com/Tyrrrz/DiscordChatExporter/blob/2.48/.docs/Using-the-CLI.md),
[export options](https://github.com/Tyrrrz/DiscordChatExporter/blob/2.48/DiscordChatExporter.Cli/Commands/Base/ExportCommandBase.cs),
[filters](https://github.com/Tyrrrz/DiscordChatExporter/blob/2.48/.docs/Message-filters.md),
[channel listing](https://github.com/Tyrrrz/DiscordChatExporter/blob/2.48/DiscordChatExporter.Cli/Commands/GetChannelsCommand.cs).

## Behavioral details that affect correctness

### A zero exit code is not proof that every channel succeeded

Upstream collects per-channel failures during a batch and only fails the command
when **all** channels fail. Its progress task also reaches 100% in a `finally`
block. Consequently, neither a batch's zero exit code nor a displayed 100% alone
proves success.

The dashboard exports exactly one channel per process with `--include-threads
None`, checks the exit code, then validates the JSON's channel ID and messages
before committing. A thread selected in the browser gets its own export and
archive. We do not silently add child threads to that process.

Source: [ExportCommandBase](https://github.com/Tyrrrz/DiscordChatExporter/blob/2.48/DiscordChatExporter.Cli/Commands/Base/ExportCommandBase.cs)
and [console progress](https://github.com/Tyrrrz/DiscordChatExporter/blob/2.48/DiscordChatExporter.Cli/Utils/Extensions/ConsoleExtensions.cs).

### Progress is an estimate over message timestamps

The engine snapshots an end message, then compares the current message's timestamp
with the start/end timestamps. It does not pre-count every message or know the
final output size. Long quiet periods and busy periods therefore advance at
different apparent speeds. Spectre.Console renders the progress; there is no
structured JSON event stream exposed by the CLI.

The dashboard labels parsed percentages as estimates and uses an indeterminate
indicator when redirected output omits them. Byte counts are measured from the
current download directory. A channel is complete only after archive commit.

Source: [DiscordClient message pagination](https://github.com/Tyrrrz/DiscordChatExporter/blob/2.48/DiscordChatExporter.Core/Discord/DiscordClient.cs).

### Forums, threads, permissions and empty channels are different cases

A forum cannot be exported as a normal text channel. The engine exports its
constituent threads. Active and archived thread discovery differ, and user/bot
accounts use different endpoints internally. The picker exposes thread discovery
explicitly; users select individual forum posts/threads. It cannot infer channel
types reliably from the CLI's plain listing, so a forum container selected as a
regular channel will report the export failure instead of claiming success.

An accessible empty channel or an empty requested date range is different: the
engine initializes an export and produces an empty file, accompanied by a warning.
A valid JSON document with `messages: []` is a successful archive result.

Available servers do not guarantee message-history permissions on every channel.
The engine resolves user/bot authentication and checks the bot MESSAGE_CONTENT
intent where necessary. It cannot recover messages no longer accessible through
Discord.

Sources: [ChannelExporter](https://github.com/Tyrrrz/DiscordChatExporter/blob/2.48/DiscordChatExporter.Core/Exporting/ChannelExporter.cs),
[DiscordClient](https://github.com/Tyrrrz/DiscordChatExporter/blob/2.48/DiscordChatExporter.Core/Discord/DiscordClient.cs).

### Media downloads are opt-in and can be incomplete

`--media` downloads referenced assets; `--reuse-media` requires it. A stable media
directory is necessary for useful reuse. The engine may keep a remote URL when an
asset download fails, so even a successful export with media enabled is not proof
that every attachment is available offline. The dashboard archives messages and attachment references by default. Settings
can enable asset downloads and reuse in a stable per-channel media folder.

The merge layer rebases existing local asset links when moving an export. Original
media files remain in place. It never concatenates rendered HTML files: JSON is the
source archive; a future HTML-reading view should consume that archive or use an
explicit upstream export action.

Sources: [ExportContext](https://github.com/Tyrrrz/DiscordChatExporter/blob/2.48/DiscordChatExporter.Core/Exporting/ExportContext.cs),
[asset downloader](https://github.com/Tyrrrz/DiscordChatExporter/blob/2.48/DiscordChatExporter.Core/Exporting/ExportAssetDownloader.cs).

### Server icons exist in metadata, not in the CLI listing

The `guilds` command prints IDs and names only. JSON exports include `guild.iconUrl`.
The picker uses an optional metadata lookup for CDN icon URLs, falls back to the
small metadata prefix of existing exports, and finally displays initials.
No credentials are sent to the browser. Normal server text channels have names
and type glyphs, not independent uploaded icons; group DMs are a separate case.

Sources: [guild listing](https://github.com/Tyrrrz/DiscordChatExporter/blob/2.48/DiscordChatExporter.Cli/Commands/GetGuildsCommand.cs),
[JSON writer](https://github.com/Tyrrrz/DiscordChatExporter/blob/2.48/DiscordChatExporter.Core/Exporting/JsonMessageWriter.cs),
[channel model](https://github.com/Tyrrrz/DiscordChatExporter/blob/2.48/DiscordChatExporter.Core/Discord/Data/Channel.cs).

## What belongs in this project

The engine should continue to own Discord authentication, pagination, thread
resolution, message serialization, rate-limit handling, and media downloading.
This project adds selection and registry management, readable status, safe staging,
per-channel identity checks, deduplication, atomic archive updates, and a local UI.

Incremental overlap captures additions and edits within that overlap. It does not
refresh all historical edits or reconcile deletions. A full-history refresh is a
separate operation. A standalone filtered export does not advance the canonical
unfiltered archive checkpoint, and interrupted-download resume must only
advance after validated commit.

The dashboard now exposes offline media, bounded parallelism, retry counts,
full-history refresh, UTC and Markdown settings. Standalone date-range/format/filter/partition exports and explicit archive versions
with media are also available. An archive reader and per-message revision browsing
remain future work. Scheduling continues to use the existing CLI. See the
[architecture and recovery guide](architecture.md).

## Release 2.48 versus the installed 2.47.3

2.48 adds poll rendering in HTML and fixes unsafe non-HTTP(S) HTML links and reverse
exports with an `after` boundary. The relevant CLI options remain compatible. The
review used a separately downloaded 2.48 binary; it did not silently overwrite a
running user's exporter installation.

[Upstream release notes](https://github.com/Tyrrrz/DiscordChatExporter/releases/tag/2.48).
