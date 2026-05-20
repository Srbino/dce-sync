# Changelog

All notable changes to this project will be documented in this file. Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `dce status` — composite health snapshot (token age, channel counts, archive size, DCE.Cli version) with `--json`, `--verify`, `--check-updates`. Exits 1 when outdated or any verify check fails, so it drops into a cron `||` branch.
- `dce list --json` — registry as `{output_dir, channels: [{name, id, last_after}]}`.
- `dce verify --json` — integrity report as `{output_dir, mode, total, ok, failed_count, files: [{name, size, status, detail}]}`.
- GitHub Actions smoke test (`.github/workflows/test.yml`) — Python 3.10/3.11/3.12 matrix; checks syntax, import, `--help` covers every subcommand, both completion scripts parse, token round-trip, parser-error formatting, sdist + wheel build.
- README CI badge and `dce status` / `--until` examples.

### Internal

- `_installed_dce_version` and `_latest_dce_version` extracted from `cmd_upgrade_check` so `dce status` can reuse them with silent (None on error) semantics while `upgrade-check` keeps its loud `die()` path.

## [0.1.0] - 2026-05-20

Initial tagged release. Wraps [DiscordChatExporter.Cli](https://github.com/Tyrrrz/DiscordChatExporter) with friendly channel names, parallel + incremental sync, archive tooling, and machine-readable output for cron / pipelines.

### Added — sync

- `dce sync [name ...]` with friendly channel names from `channels.yaml`.
- Incremental sync via `(after YYYY-MM-DD)` filename parsing — no manual bookkeeping.
- `-j` / `--jobs N` for parallel channel downloads (thread pool around DCE.Cli subprocesses, prefixed line output to stay readable when interleaved).
- `--since 7d|3w|2m|1y` to override file-based `last_after`.
- `--until YYYY-MM-DD` upper bound, mirroring `--since`; passes `--before` through to DCE.Cli.
- `--watch [SECONDS]` for periodic per-channel size/delta snapshots in parallel mode (background daemon thread).
- `-q` / `--quiet` (and `DCE_QUIET=1` env var) for cron-style runs — suppresses chatter, leaves a `synced N, failed M` summary plus any FAILED lines on stderr.
- `--retries N` with exponential backoff (`min(2^attempt, 60s)`) for transient subprocess failures.
- `--dry-run` to preview commands without exporting.

### Added — registry & discovery

- `dce list` — registry contents + last-export date per channel.
- `dce add NAME CHANNEL_ID` — register manually.
- `dce discover --guild GID` — parsed table of a server's channels with `[new]`/`[existing]` status (matched by ID against `channels.yaml`); `--filter REGEX`, `--write`, `--include-threads None|Active|All`.

### Added — archive tools

- `dce verify [--quick] [--filter REGEX]` — sanity-check every JSON in `output_dir` (`OK` / `TRUNCATED` / `CORRUPT` / `NO_MESSAGES` / `IO_ERROR` / `EMPTY`); exits non-zero on any failure.
- `dce stats [--fast] [--json]` — per-channel totals (files, msgs, size, date range) and a `TOTAL` row.
- `dce merge [--dry-run] [--keep] [name ...]` — consolidate per-channel `(after X)` files; deduped by message id, atomic write through `.tmp` + `os.replace`, retains the latest `(after X)` suffix so `parse_last_after` stays accurate.
- `dce snapshot [-o FILE] [--compress gz|none]` — bundle `channels.yaml` + every JSON export into a single tar(.gz) with a top-level `manifest.json`; refuses to write into `output_dir` to avoid recursive inclusion; atomic via `.tmp`.
- `dce search PATTERN [name ...]` — grep messages across the archive; `--regex`, `--from/--to YYYY-MM-DD`, `--author NAME`, `-n LIMIT`, `-w WIDTH`, `--json` (JSONL, one match per line, raw content preserved).
- `dce export-csv [name ...]` — dump messages to CSV (`timestamp,channel,author_id,author_name,content,reactions,reply_to,attachments`); deduped across overlapping after-files; `--from/--to`, `-o FILE`.

### Added — token & infrastructure

- `dce token set <TOKEN>` — persists to `~/.config/dce-sync/token` with mode 0600.
- `dce token show | path | rm` — inspect / locate / remove the saved token.
- `dce token age [--max-days N]` — rotation reminder; exits 1 when the token file's mtime is older than the cap.
- Token discovery order: `$DCE_TOKEN` → `~/.config/dce-sync/token` → `./.dce_token` → unencrypted DCE GUI `Settings.dat`. Encrypted (`enc:…`) Settings.dat values are skipped.
- `dce upgrade-check` — compares installed DCE.Cli version against the latest GitHub release for the host platform (macOS arm64/x64, Linux x64/arm64/arm, Windows x64/arm64/x86).
- `dce completion zsh|bash` — emits a ready-to-drop completion script (dynamic channel-name completion by reading `channels.yaml` in the current dir).
- Passthrough to DCE.Cli for any unrecognized subcommand, with `-t TOKEN` auto-injected after the subcommand.
- `pyproject.toml` with `dce = dce_sync:main` console script; supports `pip install .`, `pip install -e .`, or a direct shebang `./dce` wrapper.

### Fixed

- Passthrough now injects `-t TOKEN` *after* the subcommand instead of before, since DCE.Cli rejects the leading-flag form.

### Documentation

- Full README rewrite covering the command reference table, the sync flag block, end-to-end examples (parallel sync with retries, cron-style quiet runs, search, export-csv, merge, snapshot), and a working macOS arm64 install snippet that matches the documented `~/.local/share/dce-cli/` + shim layout.
- `channels.example.yaml` shipped as a bootstrap template.

[Unreleased]: https://github.com/srba/dce-sync/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/srba/dce-sync/releases/tag/v0.1.0
