#!/usr/bin/env python3
"""dce — thin wrapper around DiscordChatExporter.Cli.

Adds value where DCE.Cli is awkward:
  - auto-token from the GUI's Settings.dat (single source of truth)
  - friendly channel-name registry (channels.yaml)
  - smart incremental sync (parses existing filenames to infer --after)

Everything else passes through to DiscordChatExporter.Cli unchanged.

Usage:
  dce list                         show registered channels + last export date
  dce sync                         incremental sync of all registered channels
  dce sync pvm taming              sync only the named channels
  dce sync --dry-run               print commands without executing
  dce add NAME CHANNEL_ID          register a channel
  dce guilds                       passthrough (DCE.Cli)
  dce channels -g GUILD_ID         passthrough
  dce export -c CID --after DATE   passthrough (full DCE.Cli API)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import re
import shutil
import platform
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("dce: missing dependency. Install with: pip install pyyaml")


KNOWN_CMDS = {"list", "sync", "add", "token", "stats", "discover", "verify",
              "merge", "upgrade-check", "completion", "search", "export-csv"}

_DCE_GH_API = "https://api.github.com/repos/Tyrrrz/DiscordChatExporter/releases/latest"

DEFAULT_CONFIG = Path.cwd() / "channels.yaml"
TOKEN_FILE = Path.home() / ".config" / "dce-sync" / "token"

# Opportunistic fallback: read an unencrypted token from a DCE GUI Settings.dat.
# Modern DCE versions encrypt this as `enc:...` and we skip those entries.
DEFAULT_SETTINGS_PATHS = [
    Path.home() / "Library" / "Application Support" / "DiscordChatExporter" / "Settings.dat",
    Path.home() / ".config" / "DiscordChatExporter" / "Settings.dat",
]


def die(msg: str, code: int = 1) -> None:
    print(f"dce: {msg}", file=sys.stderr)
    sys.exit(code)


def find_dce_binary() -> str:
    for name in ("discordchatexporter", "DiscordChatExporter.Cli"):
        path = shutil.which(name)
        if path:
            return path
    die(
        "DiscordChatExporter.Cli not found on PATH.\n"
        "  install:  dotnet tool install -g DiscordChatExporter.Cli\n"
        "  then add ~/.dotnet/tools to PATH (or the equivalent on your shell)"
    )
    return ""  # unreachable


def _read_token_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        tok = path.read_text().strip()
    except OSError:
        return None
    return tok or None


def load_token(explicit_path: str | None) -> str:
    # 1. env var (CI / one-off invocations)
    if os.environ.get("DCE_TOKEN"):
        return os.environ["DCE_TOKEN"]

    # 2. our own persistent token (preferred), set via `dce token set`
    tok = _read_token_file(TOKEN_FILE)
    if tok:
        return tok

    # 3. project-local override
    tok = _read_token_file(Path.cwd() / ".dce_token")
    if tok:
        return tok

    # 4. opportunistic: unencrypted DCE GUI Settings.dat
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path).expanduser())
    candidates.extend(DEFAULT_SETTINGS_PATHS)
    for p in candidates:
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        tok = data.get("LastToken", "")
        if not tok or tok.startswith("enc:"):
            # `enc:` = DCE GUI encrypted it; we cannot decrypt without the GUI's
            # platform-specific key derivation. Fall through to error.
            continue
        return tok

    die(
        "no token found.\n"
        "  set one with:  dce token set <YOUR_DISCORD_TOKEN>\n"
        "  or:            export DCE_TOKEN=<YOUR_DISCORD_TOKEN>\n"
        "  (DCE GUI's Settings.dat is encrypted on this machine and cannot be "
        "read directly.)"
    )
    return ""  # unreachable


def cmd_token(rest: list[str]) -> int:
    usage = "usage: dce token (set <token> | show | path | rm)"
    if not rest or rest[0] in ("-h", "--help"):
        print(usage)
        return 0
    action = rest[0]
    if action == "set":
        if len(rest) != 2 or not rest[1]:
            die(usage)
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(rest[1])
        TOKEN_FILE.chmod(0o600)
        print(f"wrote token to {TOKEN_FILE} (mode 0600)")
        return 0
    if action == "show":
        tok = _read_token_file(TOKEN_FILE)
        if not tok:
            print(f"no token at {TOKEN_FILE}")
            return 1
        masked = f"{tok[:4]}...{tok[-4:]}" if len(tok) > 12 else "***"
        print(f"{TOKEN_FILE}: {masked} (len={len(tok)})")
        return 0
    if action == "path":
        print(TOKEN_FILE)
        return 0
    if action == "rm":
        if TOKEN_FILE.is_file():
            TOKEN_FILE.unlink()
            print(f"removed {TOKEN_FILE}")
        else:
            print(f"no token at {TOKEN_FILE}")
        return 0
    die(f"unknown token subcommand: {action}\n{usage}")
    return 1  # unreachable


def load_config(path: Path) -> dict:
    if not path.is_file():
        die(
            f"no channels file at {path}.\n"
            f"  copy channels.example.yaml to {path.name} and edit, "
            f"or pass --config PATH."
        )
    return yaml.safe_load(path.read_text()) or {}


def save_config(path: Path, cfg: dict) -> None:
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))


def parse_last_after(output_dir: Path, channel_id: str) -> date | None:
    """Scan existing JSON exports for the latest `(after YYYY-MM-DD)` marker for
    this channel. Channel ID is matched in the filename, which is how
    DiscordChatExporter names files."""
    if not output_dir.is_dir():
        return None
    pattern = re.compile(r"\(after (\d{4}-\d{2}-\d{2})\)\.json$", re.I)
    latest: date | None = None
    for f in output_dir.iterdir():
        if channel_id not in f.name:
            continue
        m = pattern.search(f.name)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if latest is None or d > latest:
            latest = d
    return latest


def output_dir_from_cfg(cfg: dict, config_path: Path) -> Path:
    raw = cfg.get("output_dir", ".")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (config_path.parent / p).resolve()
    return p


def cmd_list(cfg: dict, config_path: Path) -> int:
    output_dir = output_dir_from_cfg(cfg, config_path)
    channels = cfg.get("channels") or {}
    if not channels:
        print("no channels registered. Use `dce add NAME CHANNEL_ID`.")
        return 0
    rows = []
    for name, ch in channels.items():
        cid = str(ch["id"])
        last = parse_last_after(output_dir, cid)
        rows.append((name, cid, str(last) if last else "(none)"))
    w_n = max(len(r[0]) for r in rows)
    w_i = max(len(r[1]) for r in rows)
    print(f"output_dir: {output_dir}")
    for n, i, l in rows:
        print(f"  {n:<{w_n}}  {i:<{w_i}}  last_after={l}")
    return 0


_SINCE_RX = re.compile(r"^(\d+)\s*([dwmy])$", re.I)
_SINCE_UNIT_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}


def parse_since(spec: str) -> date:
    """Parse `7d` / `3w` / `2m` / `1y` and return the date that many units ago.
    Months and years are approximated (30d, 365d) — Discord cares about
    UTC midnight precision, not calendar boundaries."""
    m = _SINCE_RX.match(spec.strip())
    if not m:
        die(f"--since: expected like 7d, 3w, 2m, 1y (got {spec!r})")
    n = int(m.group(1))
    if n < 1:
        die("--since: value must be >= 1")
    unit = m.group(2).lower()
    return date.today() - timedelta(days=n * _SINCE_UNIT_DAYS[unit])


def _build_export_cmd(dce: str, token: str, cid: str, output_dir: Path,
                      last: date | None) -> list[str]:
    cmd = [
        dce, "export",
        "-t", token,
        "-c", cid,
        "-f", "Json",
        "-o", str(output_dir) + os.sep,
    ]
    if last:
        cmd += ["--after", last.isoformat()]
    return cmd


def _run_export(name: str, cmd: list[str], token: str, print_lock: threading.Lock,
                prefix_lines: bool, quiet: bool = False) -> int:
    """Run a single export. When `prefix_lines` is True, capture stdout/stderr
    and write each line prefixed with the channel name (used for parallel
    runs). Otherwise stream straight to the terminal so DCE.Cli's progress
    indicator stays interactive. In `quiet` mode we discard subprocess output
    entirely so a cron-driven sync stays silent on success."""
    if quiet:
        return subprocess.call(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    if not prefix_lines:
        return subprocess.call(cmd)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip()
        if not line:
            continue
        with print_lock:
            print(f"  [{name}] {line}", flush=True)
    proc.wait()
    return proc.returncode


class _ProgressTracker:
    """Background watcher for parallel sync. Polls the per-channel output file
    on disk and prints a one-line-per-active-channel snapshot every N seconds.
    Mark_start / mark_done are called from the worker threads."""

    def __init__(self, output_dir: Path, channel_ids: dict[str, str],
                 interval: float, print_lock: threading.Lock):
        self.output_dir = output_dir
        self.channel_ids = dict(channel_ids)
        self.interval = max(2.0, float(interval))
        self.print_lock = print_lock
        self._starts: dict[str, float] = {}
        self._sizes: dict[str, int] = {}
        self._done: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self.interval + 2)

    def mark_start(self, name: str) -> None:
        with self._lock:
            self._starts[name] = time.monotonic()

    def mark_done(self, name: str) -> None:
        with self._lock:
            self._done.add(name)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self._print_snapshot()

    def _print_snapshot(self) -> None:
        with self._lock:
            active = [(n, c) for n, c in self.channel_ids.items()
                      if n in self._starts and n not in self._done]
            starts = dict(self._starts)
            prev_sizes = dict(self._sizes)

        if not active:
            return

        rows: list[tuple[str, str, str, str]] = []
        new_sizes: dict[str, int] = {}
        for name, cid in active:
            files = _files_for_channel(self.output_dir, cid)
            if not files:
                continue
            latest = max(files, key=lambda p: p.stat().st_mtime)
            try:
                size = latest.stat().st_size
            except OSError:
                continue
            new_sizes[name] = size
            delta = size - prev_sizes.get(name, 0)
            rate = f"+{_human_size(delta)}" if delta > 0 else "idle"
            elapsed = int(time.monotonic() - starts[name])
            rows.append((name, _human_size(size), rate,
                         f"{elapsed // 60}:{elapsed % 60:02d}"))

        with self._lock:
            self._sizes.update(new_sizes)

        if not rows:
            return
        w = max(len(r[0]) for r in rows)
        with self.print_lock:
            stamp = datetime.now().strftime("%H:%M:%S")
            print(f"  --- progress {stamp} ---", flush=True)
            for n, s, r, t in rows:
                print(f"    [{n:<{w}}] size={s:>10}  delta={r:>10}  elapsed={t}",
                      flush=True)


def cmd_sync(cfg: dict, config_path: Path, token: str, dce: str,
             targets: list[str], dry_run: bool, jobs: int,
             since: date | None, watch: float, quiet: bool) -> int:
    output_dir = output_dir_from_cfg(cfg, config_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    channels = cfg.get("channels") or {}
    if not channels:
        die("no channels registered. Use `dce add NAME CHANNEL_ID`.")

    if jobs < 1:
        die("--jobs must be >= 1")
    if jobs > 6:
        print(
            f"  warning: --jobs={jobs} is aggressive for a single Discord "
            f"token; you may see 429s. DCE.Cli backs off automatically.",
            file=sys.stderr,
        )

    requested = targets or list(channels.keys())
    today = date.today()
    failed: list[str] = []
    queue: list[tuple[str, str, list[str], date | None]] = []

    for name in requested:
        if name not in channels:
            print(f"  {name}: unknown (try `dce list`)", file=sys.stderr, flush=True)
            failed.append(name)
            continue
        cid = str(channels[name]["id"])
        if since is not None:
            last = since
        else:
            last = parse_last_after(output_dir, cid)
        if last and last >= today:
            if not quiet:
                print(f"  {name}: up to date (last_after={last})", flush=True)
            continue
        cmd = _build_export_cmd(dce, token, cid, output_dir, last)

        if dry_run:
            redacted = ["<TOKEN>" if c == token else c for c in cmd]
            print(f"  {name}: {' '.join(redacted)}", flush=True)
            continue
        queue.append((name, cid, cmd, last))

    if not queue:
        return 1 if failed else 0

    print_lock = threading.Lock()
    prefix = jobs > 1 and not quiet
    synced = 0

    if jobs == 1:
        for name, _cid, cmd, last in queue:
            if not quiet:
                print(f"  {name}: exporting (after={last or 'beginning'})...",
                      flush=True)
            rc = _run_export(name, cmd, token, print_lock,
                             prefix_lines=False, quiet=quiet)
            if rc != 0:
                print(f"  {name}: FAILED (exit {rc})", file=sys.stderr, flush=True)
                failed.append(name)
            else:
                synced += 1
        if quiet:
            print(f"synced {synced}, failed {len(failed)}", flush=True)
        return 1 if failed else 0

    # Parallel path: announce all jobs up front, then dispatch.
    if not quiet:
        for name, _cid, _cmd, last in queue:
            print(f"  {name}: queued (after={last or 'beginning'})", flush=True)

    tracker: _ProgressTracker | None = None
    if watch > 0 and not quiet:
        tracker = _ProgressTracker(
            output_dir=output_dir,
            channel_ids={name: cid for name, cid, _, _ in queue},
            interval=watch,
            print_lock=print_lock,
        )
        tracker.start()

    def _wrapped(name: str, cmd: list[str]) -> int:
        if tracker:
            tracker.mark_start(name)
        try:
            return _run_export(name, cmd, token, print_lock, prefix, quiet=quiet)
        finally:
            if tracker:
                tracker.mark_done(name)

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
        futures = {
            ex.submit(_wrapped, name, cmd): name
            for name, _cid, cmd, _ in queue
        }
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                rc = fut.result()
            except Exception as e:  # pragma: no cover
                print(f"  [{name}] EXCEPTION: {e}", file=sys.stderr)
                failed.append(name)
                continue
            with print_lock:
                if rc == 0:
                    synced += 1
                    if not quiet:
                        print(f"  [{name}] done", flush=True)
                else:
                    print(f"  [{name}] FAILED (exit {rc})", file=sys.stderr, flush=True)
                    failed.append(name)

    if tracker:
        tracker.stop()
    if quiet:
        print(f"synced {synced}, failed {len(failed)}", flush=True)
    return 1 if failed else 0


def _verify_quick(path: Path) -> tuple[str, str | None]:
    """Sniff the tail of the file to catch the most common breakage
    (DCE crashed mid-write -> file truncated). Doesn't catch corruption
    in the middle of the file."""
    size = path.stat().st_size
    if size == 0:
        return "EMPTY", "0 bytes"
    with open(path, "rb") as fh:
        fh.seek(max(0, size - 512))
        tail = fh.read(512).decode("utf-8", errors="replace").rstrip()
    if not tail.endswith(("}", "]")):
        return "TRUNCATED", f"tail ends: ...{tail[-50:]!r}"
    return "OK", None


def cmd_verify(cfg: dict, config_path: Path, quick: bool,
               filter_re: str | None) -> int:
    output_dir = output_dir_from_cfg(cfg, config_path)
    if not output_dir.is_dir():
        die(f"output_dir does not exist: {output_dir}")

    files = sorted(output_dir.glob("*.json"))
    if filter_re:
        pat = re.compile(filter_re, re.I)
        files = [f for f in files if pat.search(f.name)]

    if not files:
        print("no JSON files matched")
        return 0

    mode = "quick (tail sniff)" if quick else "full (json.load)"
    print(f"output_dir: {output_dir}")
    print(f"checking {len(files)} files, {mode}...\n", flush=True)

    bad: list[tuple[str, str, str | None]] = []
    for fp in files:
        size = fp.stat().st_size
        if quick:
            status, detail = _verify_quick(fp)
        else:
            try:
                with open(fp) as fh:
                    data = json.load(fh)
                if "messages" not in data:
                    status, detail = "NO_MESSAGES", None
                else:
                    status, detail = "OK", f"{len(data['messages']):,} msgs"
            except json.JSONDecodeError as e:
                status, detail = "CORRUPT", str(e)[:100]
            except OSError as e:
                status, detail = "IO_ERROR", str(e)[:100]
        if status != "OK":
            bad.append((fp.name, status, detail))
        line = f"  [{status:>10}]  {_human_size(size):>10}  {fp.name}"
        if detail:
            line += f"  ({detail})"
        print(line, flush=True)

    ok = len(files) - len(bad)
    print(f"\n{ok}/{len(files)} OK", flush=True)
    if bad:
        print(f"{len(bad)} problem(s):", file=sys.stderr, flush=True)
        for n, s, d in bad:
            print(f"  {s}: {n}", file=sys.stderr, flush=True)
        return 1
    return 0


_CSV_COLUMNS = [
    "timestamp", "channel", "author_id", "author_name",
    "content", "reactions", "reply_to", "attachments",
]


def cmd_export_csv(cfg: dict, config_path: Path, targets: list[str],
                   date_from: str | None, date_to: str | None,
                   output_path: str | None) -> int:
    output_dir = output_dir_from_cfg(cfg, config_path)
    channels_cfg = cfg.get("channels") or {}
    if not channels_cfg:
        die("no channels registered")

    if not targets:
        targets = list(channels_cfg.keys())
    unknown = [n for n in targets if n not in channels_cfg]
    if unknown:
        die(f"unknown channel(s): {', '.join(unknown)}")

    if output_path:
        out_fh = open(output_path, "w", newline="", encoding="utf-8")
        close_after = True
    else:
        out_fh = sys.stdout
        close_after = False

    rows_written = 0
    try:
        writer = csv.writer(out_fh)
        writer.writerow(_CSV_COLUMNS)
        for name in targets:
            cid = str(channels_cfg[name]["id"])
            files = _files_for_channel(output_dir, cid)
            if not files:
                print(f"  warn: no files for {name}", file=sys.stderr, flush=True)
                continue
            seen: set[str] = set()
            for fp in files:
                try:
                    with open(fp) as fh:
                        data = json.load(fh)
                except (json.JSONDecodeError, OSError) as e:
                    print(f"  warn: {fp.name}: {e}", file=sys.stderr, flush=True)
                    continue
                for msg in data.get("messages") or []:
                    mid = msg.get("id") or ""
                    if mid and mid in seen:
                        continue
                    seen.add(mid)
                    ts = msg.get("timestamp") or ""
                    day = ts[:10]
                    if date_from and day < date_from:
                        continue
                    if date_to and day > date_to:
                        continue
                    author = msg.get("author") or {}
                    content = (msg.get("content") or "").replace("\r", " ").replace("\n", " ")
                    reactions = sum((r.get("count") or 0)
                                    for r in (msg.get("reactions") or []))
                    ref = msg.get("reference") or {}
                    reply_to = ref.get("messageId") or ""
                    attachments = len(msg.get("attachments") or [])
                    writer.writerow([
                        ts, name,
                        author.get("id") or "",
                        author.get("name") or "",
                        content, reactions, reply_to, attachments,
                    ])
                    rows_written += 1
    finally:
        if close_after:
            out_fh.close()

    if output_path:
        try:
            size = Path(output_path).stat().st_size
        except OSError:
            size = 0
        print(f"wrote {rows_written:,} rows to {output_path} ({_human_size(size)})",
              file=sys.stderr, flush=True)
    elif rows_written == 0:
        print("no rows matched", file=sys.stderr, flush=True)
        return 1
    return 0


def cmd_search(cfg: dict, config_path: Path, pattern: str,
               targets: list[str], use_regex: bool,
               date_from: str | None, date_to: str | None,
               author_filter: str | None, limit: int,
               width: int) -> int:
    output_dir = output_dir_from_cfg(cfg, config_path)
    channels_cfg = cfg.get("channels") or {}
    if not channels_cfg:
        die("no channels registered")

    if targets:
        unknown = [n for n in targets if n not in channels_cfg]
        if unknown:
            die(f"unknown channel(s): {', '.join(unknown)}")
        allowed = {str(channels_cfg[n]["id"]): n for n in targets}
    else:
        allowed = {str(c["id"]): n for n, c in channels_cfg.items()}

    if use_regex:
        try:
            pat = re.compile(pattern, re.I)
        except re.error as e:
            die(f"bad regex: {e}")
    else:
        pat = re.compile(re.escape(pattern), re.I)
    author_pat = re.compile(re.escape(author_filter), re.I) if author_filter else None

    files: list[tuple[Path, str]] = []
    if output_dir.is_dir():
        for fp in sorted(output_dir.glob("*.json")):
            for cid in allowed:
                if cid in fp.name:
                    files.append((fp, cid))
                    break
    if not files:
        die("no archived JSON files found")

    hits = 0
    seen: set[str] = set()  # dedupe across overlapping exports by message id
    for fp, cid in files:
        cname = allowed[cid]
        try:
            with open(fp) as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  warn: {fp.name}: {e}", file=sys.stderr, flush=True)
            continue
        for msg in data.get("messages") or []:
            content = msg.get("content") or ""
            if not content:
                continue
            ts = (msg.get("timestamp") or "")[:10]
            if date_from and ts < date_from:
                continue
            if date_to and ts > date_to:
                continue
            author = ((msg.get("author") or {}).get("name") or "")
            if author_pat and not author_pat.search(author):
                continue
            if not pat.search(content):
                continue
            mid = msg.get("id") or ""
            if mid and mid in seen:
                continue
            seen.add(mid)
            flat = re.sub(r"\s+", " ", content).strip()
            if width > 0 and len(flat) > width:
                flat = flat[:width - 1] + "…"
            print(f"{ts} | {cname[:12]:<12} | {author[:16]:<16} | {flat}",
                  flush=True)
            hits += 1
            if limit and hits >= limit:
                print(f"\n-- stopped at --limit {limit} --", file=sys.stderr,
                      flush=True)
                return 0

    if hits == 0:
        print(f"no matches for {pattern!r}", file=sys.stderr, flush=True)
        return 1
    print(f"\n-- {hits} match(es) --", file=sys.stderr, flush=True)
    return 0


def _files_for_channel(output_dir: Path, channel_id: str) -> list[Path]:
    if not output_dir.is_dir():
        return []
    return sorted(
        f for f in output_dir.iterdir()
        if f.is_file() and f.suffix.lower() == ".json" and channel_id in f.name
    )


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


def _scan_file_stats(path: Path) -> tuple[int, str | None, str | None]:
    """Return (message_count, first_iso_date, last_iso_date). Loads the full
    JSON because DCE writes messageCount only at the end of the file; a header
    seek won't get us a reliable count for partial reads."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return (0, None, None)
    msgs = data.get("messages") or []
    if not msgs:
        return (0, None, None)
    first = (msgs[0].get("timestamp") or "")[:10] or None
    last = (msgs[-1].get("timestamp") or "")[:10] or None
    return (len(msgs), first, last)


def cmd_stats(cfg: dict, config_path: Path, fast: bool) -> int:
    output_dir = output_dir_from_cfg(cfg, config_path)
    channels = cfg.get("channels") or {}
    if not channels:
        die("no channels registered. Use `dce add NAME CHANNEL_ID`.")

    print(f"output_dir: {output_dir}", flush=True)
    rows: list[tuple[str, int, int, int, str]] = []
    total_files = total_msgs = total_bytes = 0

    for name, ch in channels.items():
        cid = str(ch["id"])
        files = _files_for_channel(output_dir, cid)
        if not files:
            rows.append((name, 0, 0, 0, "(no exports)"))
            continue

        ch_bytes = sum(f.stat().st_size for f in files)
        if fast:
            rows.append((name, len(files), 0, ch_bytes, "(--fast: msgs/range skipped)"))
            total_files += len(files)
            total_bytes += ch_bytes
            continue

        print(f"  scanning {name} ({len(files)} files, {_human_size(ch_bytes)})...",
              flush=True)
        ch_msgs = 0
        first: str | None = None
        last: str | None = None
        for fp in files:
            n, fst, lst = _scan_file_stats(fp)
            ch_msgs += n
            if fst and (first is None or fst < first):
                first = fst
            if lst and (last is None or lst > last):
                last = lst
        date_range = f"{first} -> {last}" if first else "(empty)"
        rows.append((name, len(files), ch_msgs, ch_bytes, date_range))
        total_files += len(files)
        total_msgs += ch_msgs
        total_bytes += ch_bytes

    w_n = max(len(r[0]) for r in rows + [("TOTAL", 0, 0, 0, "")])
    print()
    print(f"  {'channel':<{w_n}}  {'files':>5}  {'msgs':>9}  {'size':>10}  date range")
    for n, fc, mc, b, dr in rows:
        msg_s = f"{mc:>9,}" if mc else "        -"
        print(f"  {n:<{w_n}}  {fc:>5}  {msg_s}  {_human_size(b):>10}  {dr}")
    print(f"  {'TOTAL':<{w_n}}  {total_files:>5}  {total_msgs:>9,}  "
          f"{_human_size(total_bytes):>10}")
    return 0


_AFTER_RX = re.compile(r"(?P<prefix>.*) \(after (?P<date>\d{4}-\d{2}-\d{2})\)(?P<ext>\.json)$", re.I)


def _pick_merge_target(files: list[Path]) -> Path:
    """Pick the filename for the merged archive: reuse the latest `(after X)`
    suffix among the inputs so `parse_last_after` continues to work and the
    next sync resumes from the right point."""
    latest = None
    template = files[-1]
    for fp in files:
        m = _AFTER_RX.match(fp.name)
        if not m:
            continue
        d = m.group("date")
        if latest is None or d > latest:
            latest = d
            template = fp
    return template.parent / template.name


def cmd_merge(cfg: dict, config_path: Path, targets: list[str],
              dry_run: bool, keep: bool) -> int:
    output_dir = output_dir_from_cfg(cfg, config_path)
    channels = cfg.get("channels") or {}
    if not channels:
        die("no channels registered")

    requested = targets or list(channels.keys())
    failed: list[str] = []
    touched = 0

    for name in requested:
        if name not in channels:
            print(f"  {name}: unknown", file=sys.stderr, flush=True)
            failed.append(name)
            continue
        cid = str(channels[name]["id"])
        files = _files_for_channel(output_dir, cid)
        if len(files) < 2:
            print(f"  {name}: {len(files)} file(s), nothing to merge", flush=True)
            continue

        merged: dict[str, dict] = {}
        meta: dict = {}
        raw_total = 0
        load_failed = False
        for fp in files:
            try:
                with open(fp) as fh:
                    data = json.load(fh)
            except (json.JSONDecodeError, OSError) as e:
                print(f"  {name}: failed to read {fp.name}: {e}",
                      file=sys.stderr, flush=True)
                failed.append(name)
                load_failed = True
                break
            msgs = data.get("messages") or []
            raw_total += len(msgs)
            for m in msgs:
                mid = m.get("id")
                if mid:
                    merged[mid] = m
            # keep metadata from the most recently-suffixed input
            for k, v in data.items():
                if k != "messages":
                    meta[k] = v
        if load_failed:
            continue

        sorted_msgs = sorted(merged.values(),
                             key=lambda m: m.get("timestamp", ""))
        target = _pick_merge_target(files)
        dup = raw_total - len(sorted_msgs)
        print(f"  {name}: {raw_total:,} msgs across {len(files)} files -> "
              f"{len(sorted_msgs):,} unique ({dup:,} dups)", flush=True)

        if dry_run:
            print(f"    -> would write {target.name} "
                  f"and remove {len(files) - 1} source file(s) (unless --keep)",
                  flush=True)
            touched += 1
            continue

        meta_out = dict(meta)
        meta_out["messages"] = sorted_msgs
        if "messageCount" in meta_out:
            meta_out["messageCount"] = len(sorted_msgs)

        tmp = target.with_name(target.name + ".tmp")
        try:
            with open(tmp, "w") as fh:
                json.dump(meta_out, fh, ensure_ascii=False)
            os.replace(tmp, target)
        except OSError as e:
            if tmp.exists():
                tmp.unlink()
            print(f"  {name}: write failed: {e}", file=sys.stderr, flush=True)
            failed.append(name)
            continue
        print(f"    -> {target.name} ({_human_size(target.stat().st_size)})",
              flush=True)

        if not keep:
            removed = 0
            for fp in files:
                if fp.resolve() != target.resolve():
                    fp.unlink()
                    removed += 1
            if removed:
                print(f"    -> removed {removed} source file(s) (use --keep to preserve)",
                      flush=True)
        touched += 1

    if touched == 0 and not failed:
        print("nothing to do")
    return 1 if failed else 0


def cmd_add(cfg: dict, config_path: Path, name: str, channel_id: str) -> int:
    cfg.setdefault("channels", {})
    if name in cfg["channels"]:
        print(f"  {name}: already registered as {cfg['channels'][name]['id']} — overwriting")
    cfg["channels"][name] = {"id": channel_id}
    save_config(config_path, cfg)
    print(f"added {name} -> {channel_id} (config: {config_path})")
    return 0


_ZSH_COMPLETION = r"""#compdef dce
# Install: dce completion zsh > ~/.zfunc/_dce
# Then add to ~/.zshrc:
#   fpath=(~/.zfunc $fpath); autoload -Uz compinit && compinit

_dce_channels() {
  local cy
  for cy in ./channels.yaml "$HOME/.config/dce-sync/channels.yaml"; do
    if [[ -f $cy ]]; then
      awk '/^[[:space:]]+[a-z][a-zA-Z0-9_-]*:[[:space:]]*$/ {
            gsub(":",""); gsub(/^[[:space:]]+/,""); print
          }' "$cy"
      return
    fi
  done
}

_dce() {
  local -a cmds
  cmds=(
    'list:show registered channels'
    'sync:incremental sync'
    'add:register a channel'
    'discover:list a server'\''s channels'
    'verify:sanity-check JSONs'
    'stats:per-channel totals'
    'merge:consolidate split exports'
    'token:manage stored Discord token'
    'upgrade-check:compare installed DCE.Cli vs latest GitHub release'
    'completion:print shell completion script'
    'guilds:passthrough (DCE.Cli)'
    'channels:passthrough'
    'export:passthrough'
    'exportguild:passthrough'
    'exportdm:passthrough'
    'exportall:passthrough'
  )
  if (( CURRENT == 2 )); then
    _describe -t commands 'dce command' cmds
    return
  fi
  local sub=$words[2]
  case $sub in
    sync)
      local chans
      chans=( ${(f)"$(_dce_channels)"} )
      _arguments \
        '--dry-run[preview only]' \
        '-j+[parallel jobs]:N' '--jobs=[parallel jobs]:N' \
        '--since=[NOW-X override]:spec (e.g. 7d, 2w)' \
        '--watch[size/delta snapshots]' '--watch=[size/delta snapshots]:seconds' \
        '*:channel:('"${chans[*]}"')'
      ;;
    merge)
      local chans; chans=( ${(f)"$(_dce_channels)"} )
      _arguments \
        '--dry-run[preview only]' '--keep[keep source files]' \
        '*:channel:('"${chans[*]}"')'
      ;;
    verify)
      _arguments '--quick[tail sniff]' '--filter=[regex]:regex'
      ;;
    stats)
      _arguments '--fast[size only]'
      ;;
    discover)
      _arguments \
        '--guild=[guild id]:id' \
        '--filter=[regex on name]:regex' \
        '--write[append to channels.yaml]' \
        '--include-threads=[mode]:mode:(None Active All)'
      ;;
    token)
      _values 'token action' set show path rm
      ;;
    completion)
      _values 'shell' zsh bash
      ;;
  esac
}
_dce "$@"
"""


_BASH_COMPLETION = r"""# Install: dce completion bash > ~/.bash_completion.d/dce
# Then: source ~/.bash_completion.d/dce  (or rely on bash-completion's loader)

_dce_completion() {
  local cur prev words cword
  COMPREPLY=()
  cur="${COMP_WORDS[COMP_CWORD]}"
  cword=$COMP_CWORD

  local cmds="list sync add discover verify stats merge token upgrade-check completion guilds channels export exportguild exportdm exportall"

  if (( cword == 1 )); then
    COMPREPLY=( $(compgen -W "$cmds" -- "$cur") )
    return
  fi

  local sub="${COMP_WORDS[1]}"
  local chans=""
  if [[ "$sub" == "sync" || "$sub" == "merge" ]]; then
    local cy
    for cy in ./channels.yaml "$HOME/.config/dce-sync/channels.yaml"; do
      [[ -f $cy ]] && chans=$(awk '/^[[:space:]]+[a-z][a-zA-Z0-9_-]*:[[:space:]]*$/ { gsub(":",""); gsub(/^[[:space:]]+/,""); print }' "$cy") && break
    done
  fi

  case "$sub" in
    sync)    COMPREPLY=( $(compgen -W "--dry-run -j --jobs --since --watch $chans" -- "$cur") ) ;;
    merge)   COMPREPLY=( $(compgen -W "--dry-run --keep $chans" -- "$cur") ) ;;
    verify)  COMPREPLY=( $(compgen -W "--quick --filter" -- "$cur") ) ;;
    stats)   COMPREPLY=( $(compgen -W "--fast" -- "$cur") ) ;;
    discover) COMPREPLY=( $(compgen -W "--guild --filter --write --include-threads" -- "$cur") ) ;;
    token)   COMPREPLY=( $(compgen -W "set show path rm" -- "$cur") ) ;;
    completion) COMPREPLY=( $(compgen -W "zsh bash" -- "$cur") ) ;;
  esac
}
complete -F _dce_completion dce
"""


def cmd_completion(shell: str) -> int:
    if shell == "zsh":
        sys.stdout.write(_ZSH_COMPLETION)
    elif shell == "bash":
        sys.stdout.write(_BASH_COMPLETION)
    else:
        die(f"unsupported shell: {shell} (zsh, bash)")
    return 0


def _platform_asset_substring() -> str | None:
    """Substring used in DCE.Cli release asset filenames for the current host
    (e.g. `osx-arm64`). Returns None for unsupported platforms."""
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    if sysname == "darwin":
        return "osx-arm64" if machine in ("arm64", "aarch64") else "osx-x64"
    if sysname == "linux":
        if machine in ("aarch64", "arm64"):
            return "linux-arm64"
        if machine.startswith("arm"):
            return "linux-arm"
        return "linux-x64"
    if sysname == "windows":
        if machine in ("arm64", "aarch64"):
            return "win-arm64"
        return "win-x64" if "64" in machine else "win-x86"
    return None


def _parse_version(s: str) -> tuple[int, ...]:
    out = []
    for part in s.lstrip("v").split("."):
        digits = re.match(r"\d+", part)
        if not digits:
            break
        out.append(int(digits.group()))
    return tuple(out) or (0,)


def cmd_upgrade_check(dce: str) -> int:
    try:
        r = subprocess.run([dce, "--version"], capture_output=True,
                           text=True, timeout=10)
        installed_raw = (r.stdout or r.stderr).strip().splitlines()[0]
    except (OSError, subprocess.TimeoutExpired) as e:
        die(f"could not run `{dce} --version`: {e}")
    installed = installed_raw.lstrip("v").strip()

    req = urllib.request.Request(_DCE_GH_API,
                                 headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        die(f"GitHub release lookup failed: {e}")

    latest = data.get("tag_name", "").lstrip("v")
    if not latest:
        die("GitHub response missing tag_name")

    iv = _parse_version(installed)
    lv = _parse_version(latest)
    behind = iv < lv

    print(f"installed: v{installed}")
    print(f"latest:    v{latest}  ({data.get('published_at','?')[:10]})")
    print(f"status:    {'OUTDATED' if behind else 'UP TO DATE'}")

    if not behind:
        return 0

    sub = _platform_asset_substring()
    asset = None
    if sub:
        for a in data.get("assets") or []:
            name = a.get("name", "")
            if name.startswith("DiscordChatExporter.Cli.") and sub in name \
                    and name.endswith(".zip"):
                asset = a
                break

    if not asset:
        print("\n(no Cli asset matched this platform; download manually from "
              "https://github.com/Tyrrrz/DiscordChatExporter/releases)",
              file=sys.stderr)
        return 1

    url = asset.get("browser_download_url", "")
    size = int(asset.get("size") or 0)
    print(f"\nasset:    {asset.get('name')} ({_human_size(size)})")
    print(f"download: {url}")
    print("\nsuggested install (matches this repo's existing layout):")
    print("  curl -L -o /tmp/dce-cli.zip \"" + url + "\"")
    print("  unzip -o /tmp/dce-cli.zip -d ~/.local/share/dce-cli/")
    print("  chmod +x ~/.local/share/dce-cli/DiscordChatExporter.Cli")
    return 1


def cmd_passthrough(args: list[str], token: str, dce: str) -> int:
    if "-t" not in args and "--token" not in args:
        # DCE.Cli expects `<subcommand> -t TOKEN`, not `-t TOKEN <subcommand>`.
        # Insert just after the first positional so flags land on the command.
        args = [args[0], "-t", token] + args[1:] if args else ["-t", token]
    return subprocess.call([dce] + args)


def _dce_channels_output(dce: str, token: str, guild_id: str,
                         include_threads: str) -> str:
    """Run `discordchatexporter channels` and return stdout. Caller decides
    how to parse — DCE.Cli prefers visual layout over a stable schema, so we
    treat the output as best-effort text and rely on the channel-ID column."""
    cmd = [dce, "channels", "-g", guild_id, "-t", token,
           "--include-threads", include_threads]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        die(f"DCE.Cli channels failed (exit {r.returncode}): "
            f"{(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip()).strip("-").lower()
    return s or "channel"


def cmd_discover(cfg: dict, config_path: Path, token: str, dce: str,
                 guild_id: str, filter_re: str | None, write: bool,
                 include_threads: str) -> int:
    out = _dce_channels_output(dce, token, guild_id, include_threads)

    # DCE.Cli prints one channel per line; columns vary by version but the
    # 17-20 digit snowflake ID is always there. Take the longest text on the
    # line after the ID as the channel name (categories tend to be shorter
    # and come before the name when present).
    id_rx = re.compile(r"\b(\d{17,20})\b")
    discovered: list[tuple[str, str, str]] = []  # (slug, cid, raw_name)
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        m = id_rx.search(line)
        if not m:
            continue
        cid = m.group(1)
        # Strip the id and split on `|`, `│`, or 2+ whitespace; pick the
        # rightmost non-empty token as the channel name.
        rest = (line[:m.start()] + line[m.end():]).strip()
        parts = [p.strip() for p in re.split(r"[|│]|\s{2,}", rest) if p.strip()]
        name = parts[-1] if parts else cid
        discovered.append((_slugify(name), cid, name))

    if not discovered:
        print("no channels parsed from DCE.Cli output", file=sys.stderr)
        return 1

    pat = re.compile(filter_re, re.I) if filter_re else None
    existing_ids = {str(c["id"]) for c in (cfg.get("channels") or {}).values()}
    existing_slugs = set(cfg.get("channels") or {})

    rows: list[tuple[str, str, str, str]] = []  # (status, slug, cid, raw)
    for slug, cid, raw in discovered:
        if pat and not pat.search(raw):
            continue
        if cid in existing_ids:
            status = "existing"
        else:
            base = slug
            i = 2
            while slug in existing_slugs:
                slug = f"{base}-{i}"
                i += 1
            existing_slugs.add(slug)
            status = "new"
        rows.append((status, slug, cid, raw))

    if not rows:
        print("filter matched no channels", file=sys.stderr)
        return 1

    w_s = max(len(r[1]) for r in rows)
    for st, slug, cid, raw in rows:
        print(f"  [{st:>8}]  {slug:<{w_s}}  {cid}  # {raw}")

    new_rows = [r for r in rows if r[0] == "new"]
    if not write:
        print(f"\n{len(new_rows)} new, {len(rows) - len(new_rows)} existing. "
              f"Re-run with --write to append.")
        return 0

    if not new_rows:
        print("\nnothing new to write")
        return 0
    cfg.setdefault("channels", {})
    for _, slug, cid, _ in new_rows:
        cfg["channels"][slug] = {"id": cid}
    save_config(config_path, cfg)
    print(f"\nappended {len(new_rows)} channel(s) to {config_path}")
    return 0


def split_our_flags(argv: list[str]) -> tuple[Path, str | None, list[str]]:
    """Pull --config and --settings out of argv (used by all commands).

    Returns: (config_path, settings_path, remaining_argv)."""
    config_path = DEFAULT_CONFIG
    settings_path: str | None = None
    out: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--config" and i + 1 < len(argv):
            config_path = Path(argv[i + 1]).expanduser().resolve()
            i += 2
            continue
        if a == "--settings" and i + 1 < len(argv):
            settings_path = argv[i + 1]
            i += 2
            continue
        out.append(a)
        i += 1
    return config_path, settings_path, out


HELP_TEXT = """\
dce — DiscordChatExporter.Cli wrapper with friendly channel names + incremental sync.

usage:
  dce [--config PATH] [--settings PATH] <command> [...]

commands handled by dce:
  list                          show registered channels and last export date
  sync [name ...]               incremental sync (default: all)
                                  flags: --dry-run, -j/--jobs N (parallel),
                                         --since 7d|3w|2m|1y (override last_after),
                                         --watch [SECONDS] (size/delta snapshots),
                                         -q/--quiet (cron-friendly; DCE_QUIET=1 env)
  add NAME CHANNEL_ID           add a channel to channels.yaml
  discover --guild GID [...]    list a server's channels, optionally append to channels.yaml
                                  flags: --filter REGEX, --write, --include-threads None|Active|All
  stats [--fast]                per-channel totals (files, msgs, size, date range)
  verify [--quick] [--filter R] sanity-check every JSON in output_dir parses
  search PATTERN [name ...]     grep archived messages
                                  flags: --regex, --from/--to YYYY-MM-DD,
                                         --author NAME, -n LIMIT, -w WIDTH
  export-csv [name ...]         dump messages to CSV (one row per message)
                                  flags: --from/--to YYYY-MM-DD, -o FILE
  merge [name ...]              consolidate per-channel `(after X)` files; deduped by msg id
                                  flags: --dry-run, --keep (don't delete source files)
  token set <TOKEN>             save the Discord token to ~/.config/dce-sync/token (0600)
  token show | path | rm        inspect / locate / remove the saved token
  upgrade-check                 compare installed DCE.Cli vs latest GitHub release
  completion (zsh|bash)         print shell completion script to stdout

anything else is forwarded to DiscordChatExporter.Cli with --token auto-injected:
  dce guilds
  dce channels -g GUILD_ID
  dce export -c CHANNEL_ID --after 2026-05-01
  dce exportguild -g GUILD_ID -f Json -o ./exports/

token lookup order (first hit wins):
  1. $DCE_TOKEN
  2. ~/.config/dce-sync/token   (set via `dce token set`)
  3. ./.dce_token               (project-local override)
  4. DCE GUI Settings.dat       (only if unencrypted — modern versions store `enc:...`)
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("-h", "--help"):
        print(HELP_TEXT)
        return 0

    config_path, settings_path, rest = split_our_flags(argv)
    if not rest:
        print(HELP_TEXT)
        return 0

    cmd = rest[0]
    sub_argv = rest[1:]

    # `token` is self-contained — no DCE.Cli or token-loading needed.
    if cmd == "token":
        return cmd_token(sub_argv)

    # `upgrade-check` needs the DCE binary path but no Discord token.
    if cmd == "upgrade-check":
        return cmd_upgrade_check(find_dce_binary())

    # `completion` is fully self-contained — emits a static script to stdout.
    if cmd == "completion":
        if not sub_argv or sub_argv[0] in ("-h", "--help"):
            die("usage: dce completion (zsh|bash)")
        return cmd_completion(sub_argv[0])

    # Everything else needs both the binary and the resolved token.
    token = load_token(settings_path)
    dce = find_dce_binary()

    if cmd == "list":
        cfg = load_config(config_path)
        return cmd_list(cfg, config_path)

    if cmd == "sync":
        p = argparse.ArgumentParser(prog="dce sync")
        p.add_argument("channels", nargs="*")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument(
            "-j", "--jobs", type=int, default=1,
            help="parallel channel downloads (default: 1)",
        )
        p.add_argument(
            "--since", default=None,
            help="override file-based last_after with NOW-X (e.g. 7d, 3w, 2m, 1y)",
        )
        p.add_argument(
            "--watch", type=float, nargs="?", const=10.0, default=0.0,
            metavar="SECONDS",
            help="periodic size/delta snapshot per channel (parallel mode only; "
                 "default off, --watch alone = every 10s)",
        )
        p.add_argument(
            "-q", "--quiet", action="store_true",
            help="suppress per-channel chatter; print only failures and "
                 "a final `synced N, failed M` summary (also via DCE_QUIET=1)",
        )
        a = p.parse_args(sub_argv)
        cfg = load_config(config_path)
        since = parse_since(a.since) if a.since else None
        quiet = a.quiet or bool(os.environ.get("DCE_QUIET"))
        return cmd_sync(cfg, config_path, token, dce, a.channels,
                        a.dry_run, a.jobs, since, a.watch, quiet)

    if cmd == "discover":
        p = argparse.ArgumentParser(prog="dce discover")
        p.add_argument("--guild", required=True, help="Discord guild (server) ID")
        p.add_argument("--filter", dest="filter_re", default=None,
                       help="regex applied to channel name")
        p.add_argument("--write", action="store_true",
                       help="append new channels to channels.yaml")
        p.add_argument("--include-threads", default="None",
                       choices=("None", "Active", "All"))
        a = p.parse_args(sub_argv)
        cfg = load_config(config_path) if config_path.is_file() else {}
        return cmd_discover(cfg, config_path, token, dce, a.guild,
                            a.filter_re, a.write, a.include_threads)

    if cmd == "merge":
        p = argparse.ArgumentParser(prog="dce merge")
        p.add_argument("channels", nargs="*")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--keep", action="store_true",
                       help="don't delete the per-after source files")
        a = p.parse_args(sub_argv)
        cfg = load_config(config_path)
        return cmd_merge(cfg, config_path, a.channels, a.dry_run, a.keep)

    if cmd == "export-csv":
        p = argparse.ArgumentParser(prog="dce export-csv")
        p.add_argument("channels", nargs="*",
                       help="channel name(s); default: all registered")
        p.add_argument("--from", dest="date_from", default=None,
                       metavar="YYYY-MM-DD")
        p.add_argument("--to", dest="date_to", default=None,
                       metavar="YYYY-MM-DD")
        p.add_argument("-o", "--output", default=None,
                       help="output file (default: stdout)")
        a = p.parse_args(sub_argv)
        cfg = load_config(config_path)
        return cmd_export_csv(cfg, config_path, a.channels,
                              a.date_from, a.date_to, a.output)

    if cmd == "search":
        p = argparse.ArgumentParser(prog="dce search")
        p.add_argument("pattern", help="substring (default) or regex with --regex")
        p.add_argument("channels", nargs="*",
                       help="channel name(s); default: all registered")
        p.add_argument("--regex", action="store_true",
                       help="treat pattern as a regular expression")
        p.add_argument("--from", dest="date_from", default=None,
                       metavar="YYYY-MM-DD")
        p.add_argument("--to", dest="date_to", default=None,
                       metavar="YYYY-MM-DD")
        p.add_argument("--author", default=None,
                       help="substring match on author display name")
        p.add_argument("-n", "--limit", type=int, default=0,
                       help="stop after N hits (0 = no limit)")
        p.add_argument("-w", "--width", type=int, default=200,
                       help="truncate each content line to N chars (0 = full)")
        a = p.parse_args(sub_argv)
        cfg = load_config(config_path)
        return cmd_search(cfg, config_path, a.pattern, a.channels, a.regex,
                          a.date_from, a.date_to, a.author, a.limit, a.width)

    if cmd == "verify":
        p = argparse.ArgumentParser(prog="dce verify")
        p.add_argument("--quick", action="store_true",
                       help="tail sniff only; skips full json.load")
        p.add_argument("--filter", dest="filter_re", default=None,
                       help="regex applied to filename")
        a = p.parse_args(sub_argv)
        cfg = load_config(config_path)
        return cmd_verify(cfg, config_path, a.quick, a.filter_re)

    if cmd == "stats":
        p = argparse.ArgumentParser(prog="dce stats")
        p.add_argument(
            "--fast", action="store_true",
            help="skip JSON parse: file count + bytes only (no msg/date)",
        )
        a = p.parse_args(sub_argv)
        cfg = load_config(config_path)
        return cmd_stats(cfg, config_path, a.fast)

    if cmd == "add":
        p = argparse.ArgumentParser(prog="dce add")
        p.add_argument("name")
        p.add_argument("channel_id")
        a = p.parse_args(sub_argv)
        # `add` can bootstrap channels.yaml from scratch
        cfg = {}
        if config_path.is_file():
            cfg = yaml.safe_load(config_path.read_text()) or {}
        return cmd_add(cfg, config_path, a.name, a.channel_id)

    # Passthrough: every other command goes straight to DCE.Cli.
    return cmd_passthrough(rest, token, dce)


if __name__ == "__main__":
    sys.exit(main())
