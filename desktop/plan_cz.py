#!/usr/bin/env python3
"""Czech pre-flight overview for the desktop launcher.

`dce list` answers "what is registered"; this answers the question you actually
have with your finger over the mouse button: *what is on disk already, how far
behind is each channel, what exactly will be fetched, and where does it land.*
It is read-only — nothing here talks to Discord.

The whole point is that it reuses dce_sync's own filename parsing, so the dates
printed here are the same dates `dce sync` will act on. If the two ever drift,
that is a bug in one of them, not a difference of opinion.

    plan_cz.py --config channels.yaml           # the human tree
    plan_cz.py --config channels.yaml --emit-plan   # machine-readable groups
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dce_sync import (  # noqa: E402
    _DCE_GH_API,
    TOKEN_FILE,
    _files_for_channel,
    _human_size,
    find_dce_binary,
    load_config,
    output_dir_from_cfg,
    parse_last_after,
)

# The server whose channels get pulled first. The Outlands Community Discord is
# where the active discussion moved, so it leads the tree and the sync queue;
# everything else is backfill. Override with --priority.
DEFAULT_PRIORITY = "Outlands Community"

# Discord invalidates tokens on password change and they rot on their own; past
# this many days the header nags rather than waiting for a 401 mid-sync.
TOKEN_WARN_DAYS = 60

DAYS_CZ = ["pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle"]
MONTHS_CZ = ["ledna", "února", "března", "dubna", "května", "června", "července",
             "srpna", "září", "října", "listopadu", "prosince"]

W = 88  # inner width of the framed layout


class C:
    """ANSI colours, blanked when stdout is not a terminal."""
    on = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    teal = "\033[38;5;43m" if on else ""
    dim = "\033[2m" if on else ""
    bold = "\033[1m" if on else ""
    warn = "\033[38;5;214m" if on else ""
    ok = "\033[38;5;78m" if on else ""
    red = "\033[38;5;203m" if on else ""
    off = "\033[0m" if on else ""


def plural(n: int, one: str, few: str, many: str) -> str:
    """Czech counts split three ways: 1, 2–4, and everything else."""
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def vis(s: str) -> int:
    """Printable width, ignoring the ANSI escapes we inject."""
    out, i = 0, 0
    while i < len(s):
        if s[i] == "\033":
            i = s.find("m", i) + 1
            continue
        out += 1
        i += 1
    return out


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - vis(s))


def cz_date(d: date) -> str:
    return f"{d.day}. {MONTHS_CZ[d.month - 1]} {d.year}"


def rule(title: str = "") -> None:
    if title:
        bar = "─" * max(0, W - vis(title) - 3)
        print(f"{C.teal}──{C.off} {C.bold}{title}{C.off} {C.teal}{bar}{C.off}")
    else:
        print(f"{C.teal}{'─' * W}{C.off}")


def server_of(output_dir: Path, cid: str, name: str, explicit: str | None) -> str:
    """Which Discord server a channel belongs to.

    Read it off an existing export — DCE.Cli names files
    `<Server> - <Category> - <channel> [id]...`, so the archive already knows.
    Channels with nothing downloaded yet fall back to the `oc-` naming
    convention, or to an explicit `server:` key in channels.yaml.
    """
    if explicit:
        return explicit
    for f in _files_for_channel(output_dir, cid):
        head = f.name.split(" - ", 1)[0].strip()
        if head:
            return head
    return "Outlands Community" if name.startswith("oc-") else "UO Outlands"


def target_example(output_dir: Path, cid: str, name: str, after: date | None,
                   today: date) -> str:
    """The filename this channel's next export will land under."""
    stem = None
    for f in _files_for_channel(output_dir, cid):
        stem = f.name.split(" [", 1)[0]
        break
    if stem is None:
        stem = f"<server> - <kategorie> - {name}"
    marker = f" (after {after.isoformat()})" if after else ""
    return f"{stem} [{cid}]{marker} (pulled {today.isoformat()}).json"


def collect(cfg: dict, config_path: Path) -> tuple[Path, list[dict]]:
    output_dir = output_dir_from_cfg(cfg, config_path)
    today = date.today()
    rows = []
    for name, ch in (cfg.get("channels") or {}).items():
        cid = str(ch["id"])
        last = parse_last_after(output_dir, cid)
        files = _files_for_channel(output_dir, cid)
        size = sum(f.stat().st_size for f in files)
        rows.append({
            "name": name,
            "id": cid,
            "last": last,
            "behind": (today - last).days if last else None,
            "files": len(files),
            "size": size,
            "server": server_of(output_dir, cid, name,
                                ch.get("server") if isinstance(ch, dict) else None),
            "todo": last is None or last < today,
        })
    return output_dir, rows


def grouped(rows: list[dict], priority: str) -> list[tuple[str, list[dict]]]:
    """Priority server first, then the rest alphabetically."""
    names = sorted({r["server"] for r in rows},
                   key=lambda s: (s.lower() != priority.lower(), s.lower()))
    return [(s, [r for r in rows if r["server"] == s]) for s in names]


def token_line() -> tuple[str, bool]:
    if not TOKEN_FILE.is_file():
        return (f"{C.red}chybí — spusť `dce token set <TOKEN>`{C.off}", False)
    saved = datetime.fromtimestamp(TOKEN_FILE.stat().st_mtime)
    age = (datetime.now() - saved).days
    stamp = f"uložen {saved.strftime('%d.%m.%Y')} · stáří {age} dní"
    if age > TOKEN_WARN_DAYS:
        return (f"{stamp}  {C.warn}⚠ zvaž rotaci (>{TOKEN_WARN_DAYS} dní){C.off}",
                True)
    return (f"{C.ok}{stamp}{C.off}", True)


def dce_version(check_updates: bool) -> str:
    try:
        out = subprocess.run([find_dce_binary(), "--version"], capture_output=True,
                             text=True, timeout=20)
        installed = (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception:
        return f"{C.red}nenalezen{C.off}"
    if not check_updates:
        return installed
    # Best-effort and deliberately short-fused: an offline laptop or a rate
    # limited GitHub must not hold up the export.
    try:
        from urllib.request import urlopen
        import json as _json
        with urlopen(_DCE_GH_API, timeout=4) as r:
            latest = _json.load(r).get("tag_name", "").lstrip("v")
        if latest and latest != installed.lstrip("v"):
            return (f"{installed}  {C.warn}⚠ k dispozici {latest} "
                    f"(`dce upgrade-check`){C.off}")
        if latest:
            return f"{C.ok}{installed} — nejnovější{C.off}"
    except Exception:
        pass
    return installed


def render(cfg: dict, config_path: Path, priority: str, check_updates: bool) -> int:
    output_dir, rows = collect(cfg, config_path)
    today = date.today()

    print()
    print(f"{C.teal}╭{'─' * W}╮{C.off}")
    head = f"  {C.bold}OUTLANDS DISCORD — EXPORT KONVERZACÍ{C.off}"
    stamp = f"{C.dim}{DAYS_CZ[today.weekday()]} {cz_date(today)}{C.off}  "
    print(f"{C.teal}│{C.off}{pad(head, W - vis(stamp))}{stamp}{C.teal}│{C.off}")
    print(f"{C.teal}╰{'─' * W}╯{C.off}")
    print()

    rule("KDE TO LEŽÍ")
    on_disk = [f for f in output_dir.iterdir() if f.is_file()] \
        if output_dir.is_dir() else []
    print(f"  Konverzace   {C.bold}{output_dir}{C.off}")
    print(f"               {C.dim}{len(on_disk)} "
          f"{plural(len(on_disk), 'soubor', 'soubory', 'souborů')} · "
          f"{_human_size(sum(f.stat().st_size for f in on_disk))}"
          f" — jeden JSON na kanál a dávku{C.off}")
    print(f"  Registr      {config_path}")
    print(f"  Token        {TOKEN_FILE}")
    print()

    rule("STAV")
    tok, _ = token_line()
    print(f"  Token        {tok}")
    print(f"  DCE.Cli      {dce_version(check_updates)}")
    print()

    rule("CO SE BUDE STAHOVAT")
    print()
    w_name = max([vis(r["name"]) for r in rows] + [12]) + 2
    for server, group in grouped(rows, priority):
        todo = [r for r in group if r["todo"]]
        tag = (f"  {C.warn}◆ PRIORITA{C.off}" if server.lower() == priority.lower()
               else f"  {C.dim}◇ starší{C.off}")
        print(f"  {C.bold}{C.teal}{server}{C.off}{tag}"
              f"{C.dim}  ({len(todo)} z {len(group)} k aktualizaci){C.off}")
        for i, r in enumerate(sorted(group, key=lambda r: r["name"])):
            last = "└─" if i == len(group) - 1 else "├─"
            if r["last"] is None and not r["files"]:
                have = f"{C.dim}nic{C.off}"
                act = f"{C.warn}stáhne CELOU historii{C.off}"
            elif r["last"] is None:
                # Files exist but carry no `(after X)` marker — a full-history
                # export. dce has no incremental anchor, so it re-pulls the lot.
                have = f"{C.warn}bez značky{C.off}"
                act = f"{C.warn}stáhne CELOU historii ZNOVU{C.off}"
            elif not r["todo"]:
                have = f"do {r['last'].strftime('%d.%m.%Y')}"
                act = f"{C.ok}aktuální{C.off}"
            else:
                have = f"do {r['last'].strftime('%d.%m.%Y')}"
                act = (f"{C.teal}dotáhne {r['behind']} "
                       f"{plural(r['behind'], 'den', 'dny', 'dní')}{C.off} "
                       f"{C.dim}(od {r['last'].strftime('%d.%m.')}){C.off}")
            meta = (f"{C.dim}{r['files']}× · {_human_size(r['size'])}{C.off}"
                    if r["files"] else f"{C.dim}—{C.off}")
            print(f"   {C.teal}{last}{C.off} {pad(r['name'], w_name)}"
                  f"{pad(have, 16)}{pad(meta, 20)}→  {act}")
        print()

    rule("SOUHRN")
    todo = [r for r in rows if r["todo"]]
    fresh = [r for r in rows if not r["todo"]]
    full = [r for r in todo if r["last"] is None]
    print(f"  K aktualizaci  {C.bold}{len(todo)}{C.off} "
          f"{plural(len(todo), 'kanál', 'kanály', 'kanálů')}"
          + (f"  {C.dim}(z toho {len(full)} od nuly){C.off}" if full else ""))
    if fresh:
        print(f"  Aktuální       {C.ok}{len(fresh)}{C.off} "
              f"{plural(len(fresh), 'kanál', 'kanály', 'kanálů')} "
              f"{C.dim}— přeskočí se{C.off}")
    if todo:
        ex = sorted(todo, key=lambda r: (r["server"].lower() != priority.lower(),
                                         r["name"]))[0]
        print()
        print(f"  {C.dim}Nový soubor bude vypadat takhle:{C.off}")
        print(f"  {C.dim}{output_dir}/{C.off}")
        print(f"    {C.teal}{target_example(output_dir, ex['id'], ex['name'], ex['last'], today)}{C.off}")
    print()
    return 0


def emit_plan(cfg: dict, config_path: Path, priority: str) -> int:
    """Machine-readable channel groups for the shell launcher: one line per
    server, priority first, so the sync runs in the same order the tree shows."""
    _, rows = collect(cfg, config_path)
    for server, group in grouped(rows, priority):
        todo = [r["name"] for r in sorted(group, key=lambda r: r["name"])
                if r["todo"]]
        if todo:
            print(f"{server}\t{' '.join(todo)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--config", default="channels.yaml")
    ap.add_argument("--priority", default=DEFAULT_PRIORITY)
    ap.add_argument("--emit-plan", action="store_true")
    ap.add_argument("--check-updates", action="store_true",
                    help="ask GitHub whether a newer DCE.Cli exists (4s budget)")
    args = ap.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    cfg = load_config(config_path)
    if args.emit_plan:
        return emit_plan(cfg, config_path, args.priority)
    return render(cfg, config_path, args.priority, args.check_updates)


if __name__ == "__main__":
    sys.exit(main())
